#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Reconcile stateless Forge proposals against machine-readable PR metadata."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from forge_common import read_json, write_json

MARKER = "repository-skill-forge-proposal"
MARKER_RE = re.compile(
    rf"<!--\s*{MARKER}:v1\s+(?P<payload>\{{.*?\}})\s*-->",
    re.DOTALL,
)
DECISIONS = {
    "create_skill",
    "improve_existing_skill",
    "merge_skills",
    "hold_as_pattern_only",
}
PROPOSAL_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def marker_payload(proposal: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "proposalKey": proposal.get("proposalKey"),
        "proposalVersion": proposal.get("proposalVersion"),
        "candidateIds": proposal.get("candidateIds"),
        "decision": proposal.get("decision"),
    }
    if not isinstance(payload["proposalKey"], str) or not PROPOSAL_KEY_RE.fullmatch(
        payload["proposalKey"]
    ):
        raise ValueError("proposalKey is invalid")
    if not isinstance(payload["proposalVersion"], str) or not payload["proposalVersion"]:
        raise ValueError("proposalVersion is required")
    candidate_ids = payload["candidateIds"]
    if (
        not isinstance(candidate_ids, list)
        or not candidate_ids
        or any(not isinstance(value, str) or not value for value in candidate_ids)
        or len(candidate_ids) != len(set(candidate_ids))
    ):
        raise ValueError("candidateIds must be unique non-empty strings")
    if payload["decision"] not in DECISIONS:
        raise ValueError("decision is invalid")
    payload["candidateIds"] = sorted(candidate_ids)
    return payload


def render_marker(proposal: dict[str, Any]) -> str:
    payload = marker_payload(proposal)
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"<!-- {MARKER}:v1 {serialized} -->"


def parse_marker(body: str) -> dict[str, Any] | None:
    matches = list(MARKER_RE.finditer(body))
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("PR body contains duplicate Forge proposal markers")
    payload = json.loads(matches[0].group("payload"))
    if not isinstance(payload, dict):
        raise ValueError("Forge proposal marker must contain an object")
    return marker_payload(payload)


def pr_status(pr: dict[str, Any]) -> str:
    if pr.get("mergedAt") or pr.get("merged_at"):
        return "merged"
    state = str(pr.get("state") or "").lower()
    if state == "merged":
        return "merged"
    if state == "open":
        return "open"
    if state == "closed":
        return "closed"
    raise ValueError("PR state must be open or closed")


def build_catalog(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for pr in prs:
        if not isinstance(pr, dict):
            raise ValueError("PR catalog input must contain objects")
        marker = parse_marker(str(pr.get("body") or ""))
        if marker is None:
            continue
        number = pr.get("number")
        if not isinstance(number, int) or number < 1:
            raise ValueError("marked PR requires a positive number")
        catalog.append(
            {
                **marker,
                "number": number,
                "url": str(pr.get("url") or pr.get("html_url") or ""),
                "status": pr_status(pr),
                "draft": bool(pr.get("draft")),
                "updatedAt": str(pr.get("updatedAt") or pr.get("updated_at") or ""),
            }
        )
    return sorted(catalog, key=lambda item: item["number"])


def validate_catalog_invariants(catalog: list[dict[str, Any]]) -> None:
    identities: set[tuple[str, str]] = set()
    open_keys: set[str] = set()
    for item in catalog:
        proposal_key = str(item.get("proposalKey") or "")
        proposal_version = str(item.get("proposalVersion") or "")
        identity = (proposal_key, proposal_version)
        if identity in identities:
            raise ValueError(
                "PR catalog contains duplicate proposalKey and proposalVersion"
            )
        identities.add(identity)
        if item.get("status") == "open":
            if proposal_key in open_keys:
                raise ValueError(
                    "PR catalog contains multiple open PRs for one proposalKey"
                )
            open_keys.add(proposal_key)


def reconcile(
    proposal: dict[str, Any],
    catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    validate_catalog_invariants(catalog)
    payload = marker_payload(proposal)
    if payload["decision"] == "hold_as_pattern_only":
        return {
            "allowed": False,
            "action": "hold",
            "reason": "pattern_only_decision",
        }
    matches = [
        item
        for item in catalog
        if item.get("proposalKey") == payload["proposalKey"]
    ]
    same_version = [
        item
        for item in matches
        if item.get("proposalVersion") == payload["proposalVersion"]
    ]
    if same_version:
        existing = same_version[-1]
        return {
            "allowed": False,
            "action": "skip",
            "reason": f"unchanged_{existing['status']}_proposal",
            "existingPr": existing,
        }
    merged_matches = [item for item in matches if item.get("status") == "merged"]
    if merged_matches and payload["decision"] not in {
        "improve_existing_skill",
        "merge_skills",
    }:
        return {
            "allowed": False,
            "action": "hold",
            "reason": "merged_proposal_requires_improvement_decision",
            "existingPr": merged_matches[-1],
        }
    open_matches = [item for item in matches if item.get("status") == "open"]
    if open_matches:
        existing = open_matches[-1]
        return {
            "allowed": True,
            "action": "update",
            "reason": "update_open_proposal",
            "existingPr": existing,
        }
    return {
        "allowed": True,
        "action": "create",
        "reason": (
            "eligible_skill_improvement"
            if merged_matches
            else "eligible_replacement_proposal"
            if matches
            else "eligible_new_proposal"
        ),
        **({"existingPr": matches[-1]} if matches else {}),
    }


def select(
    proposals: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    for proposal in proposals:
        rank = proposal.get("rank")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 0:
            raise ValueError("proposal rank must be a non-negative integer")
    ranked = sorted(
        proposals,
        key=lambda item: (item["rank"], str(item.get("proposalKey") or "")),
    )
    evaluations = [
        {"proposal": proposal, "reconciliation": reconcile(proposal, catalog)}
        for proposal in ranked
    ]
    selected = next(
        (
            item
            for item in evaluations
            if item["reconciliation"]["allowed"]
            and item["reconciliation"]["action"] in {"create", "update"}
        ),
        None,
    )
    if selected is not None:
        selected = {
            **selected,
            "marker": render_marker(selected["proposal"]),
        }
    return {
        "selection": selected,
        "mutationCount": 1 if selected else 0,
        "evaluations": evaluations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    marker_command = subcommands.add_parser("marker")
    marker_command.add_argument("--proposal", required=True, type=Path)
    marker_command.add_argument("--out", type=Path)

    catalog_command = subcommands.add_parser("catalog")
    catalog_command.add_argument("--prs", required=True, type=Path)
    catalog_command.add_argument("--out", required=True, type=Path)

    check_command = subcommands.add_parser("check")
    check_command.add_argument("--proposal", required=True, type=Path)
    check_command.add_argument("--catalog", required=True, type=Path)
    check_command.add_argument("--out", required=True, type=Path)

    select_command = subcommands.add_parser("select")
    select_command.add_argument("--proposals", required=True, type=Path)
    select_command.add_argument("--catalog", required=True, type=Path)
    select_command.add_argument("--out", required=True, type=Path)

    args = parser.parse_args()
    try:
        if args.command == "marker":
            proposal = read_json(args.proposal)
            if not isinstance(proposal, dict):
                raise ValueError("proposal must be an object")
            marker = render_marker(proposal)
            if args.out:
                args.out.write_text(marker + "\n", encoding="utf-8")
            else:
                print(marker)
            return
        if args.command == "catalog":
            prs = read_json(args.prs)
            if not isinstance(prs, list):
                raise ValueError("PR input must be an array")
            write_json(args.out, build_catalog(prs))
            return
        catalog = read_json(args.catalog)
        if not isinstance(catalog, list) or any(
            not isinstance(item, dict) for item in catalog
        ):
            raise ValueError("catalog must be an array of objects")
        if args.command == "check":
            proposal = read_json(args.proposal)
            if not isinstance(proposal, dict):
                raise ValueError("proposal must be an object")
            result = reconcile(proposal, catalog)
            write_json(args.out, result)
            raise SystemExit(0 if result["allowed"] else 1)
        proposals_document = read_json(args.proposals)
        proposals = (
            proposals_document.get("proposals")
            if isinstance(proposals_document, dict)
            else proposals_document
        )
        if not isinstance(proposals, list) or any(
            not isinstance(item, dict) for item in proposals
        ):
            raise ValueError("proposals must be an array of objects")
        write_json(args.out, select(proposals, catalog))
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
