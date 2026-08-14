#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Parse and render compact Repository Skill Forge state in one issue body."""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any

from forge_common import parse_timestamp, read_json, stable_hash, write_json

MARKER = "repository-skill-forge-state"
BLOCK_RE = re.compile(
    rf"^{MARKER}:v(?P<version>\d+):begin[ \t]*\r?\n"
    rf"(?P<payload>.*?)\r?\n{MARKER}:v(?P=version):end[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
BLOCK_OPEN_RE = re.compile(rf"^{MARKER}:v\d+:begin", re.MULTILINE)
FENCED_BLOCK_RE = re.compile(
    rf"^```{MARKER}:v(?P<version>\d+)[ \t]*\r?\n"
    rf"(?P<payload>.*?)\r?\n```[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
FENCED_BLOCK_OPEN_RE = re.compile(rf"^```{MARKER}:v", re.MULTILINE)
LEGACY_BLOCK_RE = re.compile(
    rf"^<!--[ \t]*{MARKER}:v(?P<version>\d+)[ \t]*\r?\n"
    rf"(?P<payload>.*?)\r?\n-->[ \t]*\r?$",
    re.DOTALL | re.MULTILINE,
)
LEGACY_BLOCK_OPEN_RE = re.compile(
    rf"^<!--[ \t]*{MARKER}:v",
    re.MULTILINE,
)
MAX_HTML_UNESCAPE_PASSES = 3
DEFAULT_MAX_BYTES = 60_000
ALLOWED_TOP_LEVEL = {
    "schemaVersion",
    "stateVersion",
    "scope",
    "cursor",
    "updatedAt",
    "observations",
    "fingerprintCatalog",
    "proposalQueue",
    "proposalHistory",
}
LEGACY_TOP_LEVEL = ALLOWED_TOP_LEVEL - {"fingerprintCatalog"}
OBSERVATION_KEYS = {
    "evidenceKey",
    "fingerprint",
    "sessionHash",
    "completedAt",
    "day",
    "outcome",
    "surface",
    "kind",
    "branchHash",
    "branchCategory",
    "pathFamilies",
    "refs",
}
QUEUE_REQUIRED_KEYS = {
    "proposalKey",
    "candidateIds",
    "proposalVersion",
    "decision",
    "rank",
    "enqueuedAt",
}
HISTORY_REQUIRED_KEYS = {
    "proposalKey",
    "candidateIds",
    "proposalVersion",
    "decision",
    "status",
    "publishedAt",
    "prRef",
    "draft",
}
PROPOSAL_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
HASH_RE = re.compile(r"^[0-9a-f]{16,64}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DECISIONS = {"create_skill", "improve_existing_skill", "merge_skills", "hold_as_pattern_only"}
HISTORY_STATUSES = {
    "open",
    "published",
    "closed",
    "merged",
    "rejected",
    "blocked",
    "held",
    "skipped",
}
RECONCILIATION_ACTIONS = {"create", "update", "skip", "hold"}
RECONCILIATION_KEYS = {
    "allowed",
    "reason",
    "action",
    "existingPrRef",
    "existingStatus",
    "existingDraft",
    "nextAllowedAt",
}
FORBIDDEN_KEY_RE = re.compile(
    r"(?:raw|command|arguments|query|artifact|username|userName|sessionId|localPath|cwd)",
    re.IGNORECASE,
)
FORBIDDEN_VALUE_RE = re.compile(
    r"(?:/(?:Users|home)/[^/\s]+|[A-Za-z]:\\Users\\|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}|"
    r"\b(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*"
    r"['\"]?(?!<|\$\{|\$[A-Z_]+)[^\s'\"]{12,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.IGNORECASE,
)
MAX_FINGERPRINT_CATALOG_ENTRIES = 64
MAX_FINGERPRINT_CATALOG_BYTES = 12_000
MAX_SIGNATURE_BYTES = 2_000


def validate_timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"issue state {field} must be a timestamp string")
    try:
        parse_timestamp(value)
    except ValueError as error:
        raise ValueError(f"issue state {field} is invalid") from error


def validate_string_ids(value: Any, field: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"issue state {field} must contain unique nonempty strings")


def validate_observations(observations: list[Any]) -> None:
    evidence_keys: set[str] = set()
    for item in observations:
        if not isinstance(item, dict) or set(item) != OBSERVATION_KEYS:
            raise ValueError("issue state contains an unsupported observation shape")
        for key in ("evidenceKey", "fingerprint", "sessionHash"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"issue state observation requires {key}")
        if not re.fullmatch(r"[0-9a-f]{24}", item["evidenceKey"]):
            raise ValueError("issue state observation has an invalid evidence key")
        if not re.fullmatch(r"[0-9a-f]{16}", item["fingerprint"]):
            raise ValueError("issue state observation has an invalid fingerprint")
        if not HASH_RE.fullmatch(item["sessionHash"]):
            raise ValueError("issue state observation has an invalid session hash")
        if item["evidenceKey"] in evidence_keys:
            raise ValueError("issue state contains duplicate evidence keys")
        evidence_keys.add(item["evidenceKey"])
        completed_at = item.get("completedAt")
        if completed_at is not None:
            validate_timestamp(completed_at, "observation completedAt")
        day = item.get("day")
        if day is not None and (not isinstance(day, str) or not DAY_RE.fullmatch(day)):
            raise ValueError("issue state observation has an invalid day")
        branch_hash = item.get("branchHash")
        if branch_hash is not None and (
            not isinstance(branch_hash, str) or not HASH_RE.fullmatch(branch_hash)
        ):
            raise ValueError("issue state observation has an invalid branch hash")
        if item.get("outcome") not in {"success", "failure", "unknown"}:
            raise ValueError("issue state observation has an invalid outcome")
        if item.get("surface") not in {"cli", "cca", "ccr"}:
            raise ValueError("issue state observation has an invalid surface")
        if item.get("kind") not in {"script", "command"}:
            raise ValueError("issue state observation has an invalid kind")
        if item.get("branchCategory") not in {"default", "other", "unknown"}:
            raise ValueError("issue state observation has an invalid branch category")
        paths = item.get("pathFamilies")
        if not isinstance(paths, list) or len(paths) > 8:
            raise ValueError("issue state observation pathFamilies are invalid")
        if any(
            not isinstance(path, str)
            or not path
            or path.startswith(("/", "~"))
            or re.match(r"^[A-Za-z]:[\\/]", path)
            or ".." in path.replace("\\", "/").split("/")
            for path in paths
        ):
            raise ValueError("issue state observation contains a local path")
        refs = item.get("refs")
        if not isinstance(refs, list) or len(refs) > 8:
            raise ValueError("issue state observation refs are invalid")
        for ref in refs:
            if (
                not isinstance(ref, dict)
                or set(ref) != {"type", "value"}
                or ref.get("type") not in {"pr", "issue", "commit"}
                or not isinstance(ref.get("value"), str)
                or not ref["value"]
            ):
                raise ValueError("issue state observation contains an invalid ref")


def validate_fingerprint_catalog(catalog: Any) -> None:
    if not isinstance(catalog, dict):
        raise ValueError("issue state fingerprintCatalog must be an object")
    if len(catalog) > MAX_FINGERPRINT_CATALOG_ENTRIES:
        raise ValueError("issue state fingerprintCatalog exceeds its entry limit")
    serialized_bytes = len(
        json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()
    )
    if serialized_bytes > MAX_FINGERPRINT_CATALOG_BYTES:
        raise ValueError("issue state fingerprintCatalog exceeds its size limit")
    for fingerprint, entry in catalog.items():
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{16}", fingerprint
        ):
            raise ValueError("issue state fingerprintCatalog contains an invalid key")
        if not isinstance(entry, dict) or set(entry) != {
            "kind",
            "signatureVersion",
            "signature",
            "lastSeenAt",
        }:
            raise ValueError(
                "issue state fingerprintCatalog contains an unsupported entry"
            )
        kind = entry.get("kind")
        signature = entry.get("signature")
        if kind not in {"command", "script"}:
            raise ValueError("issue state fingerprintCatalog contains an invalid kind")
        if (
            isinstance(entry.get("signatureVersion"), bool)
            or entry.get("signatureVersion") != 1
        ):
            raise ValueError(
                "issue state fingerprintCatalog contains an unsupported signature version"
            )
        if not isinstance(signature, dict) or not signature:
            raise ValueError(
                "issue state fingerprintCatalog requires a nonempty signature"
            )
        signature_bytes = len(
            json.dumps(signature, sort_keys=True, separators=(",", ":")).encode()
        )
        if signature_bytes > MAX_SIGNATURE_BYTES:
            raise ValueError("issue state fingerprintCatalog signature is too large")
        if kind == "command":
            if set(signature) != {"tokens"}:
                raise ValueError(
                    "issue state command fingerprint requires a token signature"
                )
            tokens = signature.get("tokens")
            if (
                not isinstance(tokens, list)
                or not tokens
                or len(tokens) > 40
                or any(
                    not isinstance(token, str) or not token or len(token) > 80
                    for token in tokens
                )
            ):
                raise ValueError(
                    "issue state command fingerprint has invalid tokens"
                )
        else:
            if set(signature) != {"imports", "calls", "fileExtensions"}:
                raise ValueError(
                    "issue state script fingerprint has an invalid signature"
                )
            for key in ("imports", "calls", "fileExtensions"):
                values = signature.get(key)
                if (
                    not isinstance(values, list)
                    or len(values) > 64
                    or any(
                        not isinstance(value, str) or not value or len(value) > 200
                        for value in values
                    )
                ):
                    raise ValueError(
                        f"issue state script fingerprint has invalid {key}"
                    )
        if stable_hash({"kind": kind, "signature": signature}) != fingerprint:
            raise ValueError(
                "issue state fingerprintCatalog signature does not match its key"
            )
        validate_timestamp(entry.get("lastSeenAt"), "fingerprint lastSeenAt")


def validate_reconciliation(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not set(value).issubset(RECONCILIATION_KEYS):
        raise ValueError("issue state proposal reconciliation has an unsupported shape")
    if not {"allowed", "reason", "action"}.issubset(value):
        raise ValueError("issue state proposal reconciliation is incomplete")
    if not isinstance(value.get("allowed"), bool):
        raise ValueError("issue state proposal reconciliation allowed must be boolean")
    if not isinstance(value.get("reason"), str) or not value["reason"]:
        raise ValueError("issue state proposal reconciliation requires a reason")
    if value.get("action") not in RECONCILIATION_ACTIONS:
        raise ValueError("issue state proposal reconciliation has an invalid action")
    if "existingPrRef" in value and not isinstance(value["existingPrRef"], str):
        raise ValueError("issue state proposal reconciliation existingPrRef must be a string")
    if "existingStatus" in value and value["existingStatus"] not in {
        "open",
        "closed",
        "merged",
    }:
        raise ValueError("issue state proposal reconciliation has an invalid existing status")
    if "existingDraft" in value and not isinstance(value["existingDraft"], bool):
        raise ValueError("issue state proposal reconciliation existingDraft must be boolean")
    if "nextAllowedAt" in value:
        validate_timestamp(value["nextAllowedAt"], "proposal reconciliation nextAllowedAt")


def validate_proposal_queue(queue: list[Any]) -> None:
    proposal_keys: set[str] = set()
    prior_order: tuple[int, str] | None = None
    for item in queue:
        if not isinstance(item, dict):
            raise ValueError("issue state proposal queue entries must be objects")
        if not QUEUE_REQUIRED_KEYS.issubset(item) or not set(item).issubset(
            QUEUE_REQUIRED_KEYS | {"reconciliation"}
        ):
            raise ValueError("issue state contains an unsupported proposal queue shape")
        proposal_key = item.get("proposalKey")
        if not isinstance(proposal_key, str) or not PROPOSAL_KEY_RE.fullmatch(proposal_key):
            raise ValueError("issue state proposal queue contains an invalid key")
        if proposal_key in proposal_keys:
            raise ValueError("issue state proposal queue contains duplicate keys")
        proposal_keys.add(proposal_key)
        rank = item.get("rank")
        if not isinstance(rank, int) or rank < 0:
            raise ValueError("issue state proposal queue contains an invalid rank")
        current_order = (rank, proposal_key)
        if prior_order is not None and current_order < prior_order:
            raise ValueError("issue state proposal queue is not deterministically ordered")
        prior_order = current_order
        validate_string_ids(item.get("candidateIds"), "queued proposal candidateIds")
        if not isinstance(item.get("proposalVersion"), str) or not item["proposalVersion"]:
            raise ValueError("issue state queued proposal requires a proposal version")
        if item.get("decision") not in DECISIONS:
            raise ValueError("issue state queued proposal has an invalid decision")
        validate_timestamp(item.get("enqueuedAt"), "queued proposal enqueuedAt")
        validate_reconciliation(item.get("reconciliation"))


def validate_proposal_history(history: dict[str, Any]) -> None:
    for proposal_key, item in history.items():
        if not PROPOSAL_KEY_RE.fullmatch(proposal_key) or not isinstance(item, dict):
            raise ValueError("issue state proposal history contains an invalid entry")
        if not HISTORY_REQUIRED_KEYS.issubset(item) or not set(item).issubset(
            HISTORY_REQUIRED_KEYS | {"lastEvaluation"}
        ):
            raise ValueError("issue state contains an unsupported proposal history shape")
        if item.get("proposalKey") != proposal_key:
            raise ValueError("issue state proposal history key does not match its entry")
        validate_string_ids(item.get("candidateIds"), "proposal history candidateIds")
        if not isinstance(item.get("proposalVersion"), str) or not item["proposalVersion"]:
            raise ValueError("issue state proposal history requires a proposal version")
        if item.get("decision") not in DECISIONS:
            raise ValueError("issue state proposal history has an invalid decision")
        if item.get("status") not in HISTORY_STATUSES:
            raise ValueError("issue state proposal history has an invalid status")
        validate_timestamp(item.get("publishedAt"), "proposal history publishedAt")
        if item.get("prRef") is not None and not isinstance(item["prRef"], str):
            raise ValueError("issue state proposal history prRef must be a string or null")
        if not isinstance(item.get("draft"), bool):
            raise ValueError("issue state proposal history draft must be boolean")
        evaluation = item.get("lastEvaluation")
        if evaluation is None:
            continue
        if not isinstance(evaluation, dict) or set(evaluation) != {
            "candidateIds",
            "proposalVersion",
            "decision",
            "status",
            "evaluatedAt",
        }:
            raise ValueError("issue state proposal history lastEvaluation is invalid")
        validate_string_ids(
            evaluation.get("candidateIds"),
            "proposal history lastEvaluation candidateIds",
        )
        if (
            not isinstance(evaluation.get("proposalVersion"), str)
            or not evaluation["proposalVersion"]
        ):
            raise ValueError(
                "issue state proposal history lastEvaluation requires a proposal version"
            )
        if evaluation.get("decision") not in DECISIONS:
            raise ValueError(
                "issue state proposal history lastEvaluation has an invalid decision"
            )
        if evaluation.get("status") not in HISTORY_STATUSES:
            raise ValueError("issue state proposal history lastEvaluation has an invalid status")
        validate_timestamp(
            evaluation.get("evaluatedAt"),
            "proposal history lastEvaluation evaluatedAt",
        )


def validate_sanitized_value(value: Any, key: str | None = None) -> None:
    if key and FORBIDDEN_KEY_RE.search(key):
        raise ValueError(f"issue state contains forbidden field: {key}")
    if isinstance(value, str):
        if FORBIDDEN_VALUE_RE.search(value):
            raise ValueError("issue state contains a local path or secret-shaped value")
        return
    if isinstance(value, list):
        for item in value:
            validate_sanitized_value(item)
        return
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            if not isinstance(child_key, str):
                raise ValueError("issue state object keys must be strings")
            validate_sanitized_value(child_value, child_key)
        return
    if value is not None and not isinstance(value, (bool, int, float)):
        raise ValueError("issue state contains an unsupported value")


def validate_state(state: Any, repository: str) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("issue state must be a JSON object")
    if frozenset(state) not in {
        frozenset(ALLOWED_TOP_LEVEL),
        frozenset(LEGACY_TOP_LEVEL),
    }:
        raise ValueError("issue state has an unsupported top-level shape")
    state = dict(state)
    state.setdefault("fingerprintCatalog", {})
    if state.get("schemaVersion") != 2:
        raise ValueError("unsupported issue state schema version")
    if not isinstance(state.get("stateVersion"), int) or state["stateVersion"] < 1:
        raise ValueError("issue state version must be a positive integer")
    if state.get("scope") != {"kind": "repository", "repository": repository}:
        raise ValueError("issue state repository does not exactly match")
    cursor = state.get("cursor")
    if cursor is not None:
        validate_timestamp(cursor, "cursor")
    validate_timestamp(state.get("updatedAt"), "updatedAt")
    if not isinstance(state.get("observations"), list):
        raise ValueError("issue state observations must be an array")
    if not isinstance(state.get("proposalQueue"), list):
        raise ValueError("issue state proposalQueue must be an array")
    if not isinstance(state.get("proposalHistory"), dict):
        raise ValueError("issue state proposalHistory must be an object")
    validate_observations(state["observations"])
    validate_fingerprint_catalog(state["fingerprintCatalog"])
    validate_proposal_queue(state["proposalQueue"])
    validate_proposal_history(state["proposalHistory"])
    validate_sanitized_value(state)
    return state


def parse_body(body: str, repository: str, max_bytes: int) -> dict[str, Any]:
    if len(body.encode("utf-8")) > max_bytes:
        raise ValueError("issue body exceeds the configured serialized-size ceiling")
    matches = (
        [(match, 2) for match in BLOCK_RE.finditer(body)]
        + [(match, 1) for match in FENCED_BLOCK_RE.finditer(body)]
        + [(match, 1) for match in LEGACY_BLOCK_RE.finditer(body)]
    )
    marker_count = (
        len(BLOCK_OPEN_RE.findall(body))
        + len(FENCED_BLOCK_OPEN_RE.findall(body))
        + len(LEGACY_BLOCK_OPEN_RE.findall(body))
    )
    if len(matches) != 1 or marker_count != 1:
        raise ValueError("issue body must contain exactly one well-formed state block")
    match, supported_version = matches[0]
    if int(match.group("version")) != supported_version:
        raise ValueError("unsupported issue state marker version")
    payload = match.group("payload")
    error: json.JSONDecodeError | None = None
    for attempt in range(MAX_HTML_UNESCAPE_PASSES + 1):
        try:
            return validate_state(json.loads(payload), repository)
        except json.JSONDecodeError as current_error:
            error = current_error
        if attempt == MAX_HTML_UNESCAPE_PASSES:
            break
        decoded_payload = html.unescape(payload)
        if decoded_payload == payload:
            break
        payload = decoded_payload
    raise ValueError("issue state block contains malformed JSON") from error


def render_body(
    state: dict[str, Any],
    repository: str,
    max_bytes: int,
    existing_body: str | None,
) -> str:
    payload = json.dumps(
        validate_state(state, repository),
        sort_keys=True,
        separators=(",", ":"),
    )
    block = f"{MARKER}:v2:begin\n{payload}\n{MARKER}:v2:end"
    if existing_body is None:
        human = (
            "# Repository Skill Forge state\n\n"
            "This issue stores compact machine-managed Forge state. "
            "Preserve this issue and the `skills-forge` label."
        )
        rendered = f"{human}\n\n{block}\n"
    else:
        matches = (
            [(match, 2) for match in BLOCK_RE.finditer(existing_body)]
            + [(match, 1) for match in FENCED_BLOCK_RE.finditer(existing_body)]
            + [(match, 1) for match in LEGACY_BLOCK_RE.finditer(existing_body)]
        )
        marker_count = (
            len(BLOCK_OPEN_RE.findall(existing_body))
            + len(FENCED_BLOCK_OPEN_RE.findall(existing_body))
            + len(LEGACY_BLOCK_OPEN_RE.findall(existing_body))
        )
        if matches:
            if len(matches) != 1 or marker_count != 1:
                raise ValueError("existing issue body has duplicate or malformed state blocks")
            match, supported_version = matches[0]
            if int(match.group("version")) != supported_version:
                raise ValueError("unsupported issue state marker version")
            rendered = (
                existing_body[: match.start()]
                + block
                + existing_body[match.end() :]
            )
        elif marker_count:
            raise ValueError("existing issue body has a malformed state marker")
        else:
            separator = "" if existing_body.endswith("\n") else "\n"
            rendered = f"{existing_body}{separator}\n{block}\n"
    if len(rendered.encode("utf-8")) > max_bytes:
        raise ValueError("rendered issue body exceeds the configured serialized-size ceiling")
    return rendered


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    parse_command = subparsers.add_parser("parse")
    parse_command.add_argument("--body-in", required=True, type=Path)
    parse_command.add_argument("--state-out", required=True, type=Path)
    parse_command.add_argument("--repository", required=True)
    parse_command.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)

    render_command = subparsers.add_parser("render")
    render_command.add_argument("--state-in", required=True, type=Path)
    render_command.add_argument("--body-in", type=Path)
    render_command.add_argument("--body-out", required=True, type=Path)
    render_command.add_argument("--repository", required=True)
    render_command.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    if args.max_bytes < 1:
        raise SystemExit("--max-bytes must be positive")
    try:
        if args.command == "parse":
            body = args.body_in.read_text(encoding="utf-8")
            write_json(args.state_out, parse_body(body, args.repository, args.max_bytes))
            return
        state = read_json(args.state_in)
        existing = args.body_in.read_text(encoding="utf-8") if args.body_in else None
        args.body_out.write_text(
            render_body(state, args.repository, args.max_bytes, existing),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
