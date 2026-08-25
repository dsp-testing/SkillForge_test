#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Normalize and sanitize newly completed Forge extraction batches."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any

from forge_common import read_json, write_json

SCRIPT_DIR = Path(__file__).resolve().parent


def load_script(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


normalizer = load_script("forge_normalize_sessions", "normalize-sessions.py")
deriver = load_script("forge_derive_primitives", "derive-primitives.py")
sanitizer = load_script("forge_sanitize_evidence", "sanitize-evidence.py")
merger = load_script("forge_merge_sanitized", "merge-sanitized-primitives.py")


def read_rows(paths: list[str], kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        page = read_json(path)
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise ValueError(f"{kind} artifact must be a JSON array of objects: {path}")
        rows.extend(page)
    return rows


def completed_batches(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        batch
        for partition in state.get("partitions", [])
        if isinstance(partition, dict)
        for batch in partition.get("batches", [])
        if isinstance(batch, dict) and batch.get("status") == "complete"
    ]


def batch_artifact_paths(batch: dict[str, Any]) -> list[str]:
    paths = []
    metadata = batch.get("metadataArtifact")
    if isinstance(metadata, str) and metadata:
        paths.append(metadata)
    for key in ("refsArtifacts", "filesArtifacts", "toolArtifacts"):
        values = batch.get(key)
        if isinstance(values, list):
            paths.extend(str(value) for value in values if value)
    return paths


def cleanup_raw_artifacts(
    run_dir: Path,
    batch: dict[str, Any],
    generated_paths: list[Path],
) -> None:
    candidates = [Path(path) for path in batch_artifact_paths(batch)]
    for path in list(candidates):
        if path.name.endswith(".accepted.json"):
            candidates.append(path.with_name(path.name.removesuffix(".accepted.json") + ".json"))
    candidates.extend(generated_paths)
    resolved_run_dir = run_dir.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(resolved_run_dir):
            raise ValueError(f"refusing to delete artifact outside runDir: {candidate}")
        if resolved.is_file():
            resolved.unlink()


def assert_sanitized_invariants(sanitized: dict[str, Any], batch_id: str) -> None:
    """Defense-in-depth check that the sanitizer actually produced a safe document.

    An *advisory* finding (currently only `assigned_secret`) is never
    batch-fatal on its own: the sanitizer already strips raw evidence and
    deterministically redacts every command template and signature before
    this function runs. A *blocking* finding (a concrete credential shape
    such as `github_token`, `aws_access_key`, `private_key`, or
    `bearer_token`) is handled separately in `checkpoint` and always fails
    the batch, regardless of what this function finds.

    This function only guards against the sanitizer itself regressing, so a
    genuine safety-invariant failure still blocks the batch instead of
    silently promoting an unsafe document.
    """
    sanitization = sanitized.get("sanitization")
    if not isinstance(sanitization, dict) or sanitization.get("rawSourceContentRetained") is not False:
        raise ValueError(
            f"completed batch {batch_id} sanitized document failed the raw-content invariant"
        )
    primitives = sanitized.get("primitives")
    if not isinstance(primitives, list):
        raise ValueError(f"completed batch {batch_id} sanitized document is missing primitives")
    for primitive in primitives:
        if not isinstance(primitive, dict):
            raise ValueError(f"completed batch {batch_id} sanitized document has a malformed primitive")
        if "rawEvidence" in primitive:
            raise ValueError(
                f"completed batch {batch_id} sanitized primitive still retains rawEvidence"
            )
        if primitive.get("sourceContentRetained") is not False:
            raise ValueError(
                f"completed batch {batch_id} sanitized primitive failed the source-content invariant"
            )

    # Content-safety backstop: recursively scan every string in the
    # sanitized document for a surviving TOKEN_PATTERNS match. This must
    # never fire in practice once redaction is correct; it exists to catch a
    # future field that gets sanitized independently of the rest of the
    # document (the exact shape of the `signature.tokens` regression this
    # guards against) rather than derived from already-redacted content. It
    # is not a substitute for `redact()` consuming an entire sensitive span
    # (for example a multi-line PEM body): a regex that matches only part of
    # a secret would let this scan pass even though the value it does not
    # cover remains raw.
    leaked_kinds = sanitizer.token_pattern_matches(sanitized)
    if leaked_kinds:
        raise ValueError(
            f"completed batch {batch_id} sanitized document still contains a "
            f"matched pattern ({sorted(set(leaked_kinds))})"
        )


def checkpoint(
    state: dict[str, Any],
    *,
    ledger_path: Path,
    main_branches: set[str],
) -> dict[str, Any]:
    scope = state.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("extraction state is missing scope")
    repository = scope.get("repository")
    window_start = scope.get("windowStart")
    window_end = scope.get("windowEnd")
    run_dir_value = state.get("runDir")
    if not all(isinstance(value, str) and value for value in (
        repository,
        window_start,
        window_end,
        run_dir_value,
    )):
        raise ValueError("extraction state has incomplete scope or runDir")
    run_dir = Path(run_dir_value)
    if not ledger_path.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("sanitized ledger must be inside runDir")
    ledger = read_json(ledger_path) if ledger_path.is_file() else None
    if ledger is not None and not isinstance(ledger, dict):
        raise ValueError("sanitized ledger must be a JSON object")
    if ledger is not None:
        ledger_scope = ledger.get("scope")
        if (
            not isinstance(ledger_scope, dict)
            or ledger_scope.get("repository") != repository
        ):
            raise ValueError("sanitized ledger repository does not match extraction state")
    processed = {
        str(value)
        for value in (ledger or {}).get("processedBatchIds", [])
        if value
    }
    checkpointed: list[str] = []
    finding_count = 0
    blocking_finding_count = 0
    advisory_finding_count = 0
    findings_by_kind: dict[str, int] = {}
    batches_with_findings: list[str] = []

    for batch in completed_batches(state):
        batch_id = str(batch.get("batchId") or "")
        if not batch_id:
            raise ValueError("completed batch is missing batchId")
        batch_dir = run_dir / "batches" / batch_id
        normalized_path = batch_dir / "normalized-sessions.json"
        raw_path = batch_dir / "primitives.raw.json"
        sanitized_path = batch_dir / "primitives.sanitized.json"
        report_path = batch_dir / "leakage-report.json"
        if batch_id in processed:
            cleanup_raw_artifacts(run_dir, batch, [normalized_path, raw_path])
            continue
        metadata_path = batch.get("metadataArtifact")
        if not isinstance(metadata_path, str) or not metadata_path:
            raise ValueError(f"completed batch {batch_id} is missing metadata")
        metadata_rows = read_rows([metadata_path], "metadata")
        refs = read_rows(
            [str(path) for path in batch.get("refsArtifacts", [])],
            "refs",
        )
        files = read_rows(
            [str(path) for path in batch.get("filesArtifacts", [])],
            "files",
        )
        tools = read_rows(
            [str(path) for path in batch.get("toolArtifacts", [])],
            "tools",
        )
        normalized = normalizer.normalize_batched_rows(
            metadata_rows,
            refs,
            files,
            tools,
            repository=repository,
            window_start=window_start,
            window_end=window_end,
            limit_sessions=len(batch.get("sessionIds", [])),
        )
        coverage = normalized.get("coverage", {})
        if coverage.get("truncated"):
            raise ValueError(f"completed batch {batch_id} normalized as truncated")
        if coverage.get("extractionErrorCount"):
            raise ValueError(f"completed batch {batch_id} contains extraction errors")
        write_json(normalized_path, normalized)

        derived = deriver.derive(normalized)
        write_json(raw_path, derived)
        sanitized, report = sanitizer.sanitize(
            derived,
            main_branches=main_branches,
        )
        write_json(sanitized_path, sanitized)
        write_json(report_path, report)

        # A blocking finding (a concrete credential shape: github_token,
        # aws_access_key, private_key, bearer_token) still fails the batch
        # closed. Only an advisory finding (assigned_secret, the heuristic
        # responsible for false positives) is promotable, and only once the
        # document below passes every remaining safety invariant.
        if report.get("blockingFindingCount"):
            raise ValueError(f"completed batch {batch_id} contains blocking leakage")
        assert_sanitized_invariants(sanitized, batch_id)

        # An advisory finding does not abort the batch: the document above
        # already satisfies the safety invariants, so record the counts and
        # kinds as diagnostics (never the matched text) and keep promoting
        # the safe sanitized primitives.
        finding_count += int(report.get("findingCount") or 0)
        blocking_finding_count += int(report.get("blockingFindingCount") or 0)
        advisory_finding_count += int(report.get("advisoryFindingCount") or 0)
        for kind, count in (report.get("findingsByKind") or {}).items():
            findings_by_kind[kind] = findings_by_kind.get(kind, 0) + int(count)
        if report.get("findingCount"):
            batches_with_findings.append(batch_id)

        ledger = merger.merge([(batch_id, sanitized)], ledger)
        write_json(ledger_path, ledger)
        processed.add(batch_id)
        checkpointed.append(batch_id)
        cleanup_raw_artifacts(
            run_dir,
            batch,
            [normalized_path, raw_path],
        )

    if state.get("status") in {"complete", "partial"}:
        ledger = merger.merge([], ledger)
        merger.apply_extraction_coverage(ledger, state)
        write_json(ledger_path, ledger)

    completed = completed_batches(state)
    return {
        "checkpointedBatchIds": checkpointed,
        "processedBatchIds": sorted(processed),
        "ledgerPath": str(ledger_path),
        "terminalCoverageAttached": state.get("status") in {"complete", "partial"},
        "checkpointedBatchCount": len(checkpointed),
        "processedBatchCount": len(processed),
        "completedBatchCount": len(completed),
        "ledgerBytes": ledger_path.stat().st_size if ledger_path.is_file() else 0,
        "extractionStatus": state.get("status"),
        "findingDiagnostics": {
            "findingCount": finding_count,
            "blockingFindingCount": blocking_finding_count,
            "advisoryFindingCount": advisory_finding_count,
            "findingsByKind": findings_by_kind,
            "batchesWithFindings": sorted(batches_with_findings),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--main-branch", action="append", default=["main", "master"])
    parser.add_argument("--out")
    args = parser.parse_args()
    state = read_json(args.state)
    if not isinstance(state, dict):
        raise SystemExit("state must be a JSON object")
    try:
        result = checkpoint(
            state,
            ledger_path=Path(args.ledger),
            main_branches=set(args.main_branch),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error
    if args.out:
        write_json(args.out, result)
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
