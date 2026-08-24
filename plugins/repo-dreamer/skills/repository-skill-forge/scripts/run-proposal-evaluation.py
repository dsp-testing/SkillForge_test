#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Run the Forge proposal evaluation controller through terminal status."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from forge_common import read_json, stable_hash, write_json
from vally_results import load_trial_records, normalize

SCRIPT_DIR = Path(__file__).resolve().parent
CONTROLLER_SPEC = importlib.util.spec_from_file_location(
    "proposal_eval_controller",
    SCRIPT_DIR / "proposal-eval-controller.py",
)
assert CONTROLLER_SPEC is not None and CONTROLLER_SPEC.loader is not None
controller = importlib.util.module_from_spec(CONTROLLER_SPEC)
CONTROLLER_SPEC.loader.exec_module(controller)

SECRET_ENV_RE = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|CREDENTIAL)",
    re.IGNORECASE,
)
VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def resolve_executable(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.parent != Path(".") or candidate.is_absolute():
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise ValueError(f"executable does not exist: {resolved}")
        return str(resolved)
    found = shutil.which(value)
    if not found:
        raise ValueError(f"executable is not on PATH: {value}")
    return found


def safe_environment(allowed: set[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in os.environ.items():
        if SECRET_ENV_RE.search(key) and key not in allowed:
            continue
        environment[key] = value
    environment["VALLY_TELEMETRY_OPTOUT"] = "1"
    environment["DO_NOT_TRACK"] = "1"
    return environment


def check_vally_version(
    executable: str,
    required_version: str,
    environment: dict[str, str],
) -> str:
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        check=False,
        text=True,
        env=environment,
        timeout=30,
    )
    text = f"{result.stdout}\n{result.stderr}"
    match = VERSION_RE.search(text)
    if result.returncode != 0 or match is None:
        raise ValueError("unable to determine Vally version")
    actual = match.group(1)
    if actual != required_version:
        raise ValueError(
            f"Vally version mismatch: expected {required_version}, found {actual}"
        )
    return actual


def find_results_jsonl(output_dir: Path) -> Path:
    matches = sorted(
        output_dir.rglob("results.jsonl"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one Vally results.jsonl under {output_dir}, "
            f"found {len(matches)}"
        )
    return matches[0]


def action_environment(
    base: dict[str, str],
    action_root: Path,
    copilot_home: Path | None,
) -> dict[str, str]:
    environment = dict(base)
    if copilot_home is None:
        return environment
    config = copilot_home.expanduser().resolve() / "config.json"
    if not config.is_file():
        raise ValueError(f"Copilot login config does not exist: {config}")
    isolated_home = action_root / "copilot-home"
    isolated_home.mkdir(parents=True, exist_ok=False)
    shutil.copy2(config, isolated_home / "config.json")
    environment["COPILOT_HOME"] = str(isolated_home)
    environment["EVALUATE_USE_HOST_COPILOT_HOME"] = "1"
    return environment


def run_vally(
    action: dict[str, Any],
    *,
    executable: str,
    repository: Path,
    workspace_root: Path,
    model: str | None,
    judge_model: str | None,
    timeout_seconds: int,
    environment: dict[str, str],
    copilot_home: Path | None,
) -> dict[str, Any]:
    action_id = action["actionId"]
    action_root = workspace_root / action_id
    if action_root.exists():
        raise ValueError(f"evaluation action directory already exists: {action_root}")
    work_dir = action_root / "work"
    workspace_dir = action_root / "workspace"
    output_dir = action_root / "results"
    for path in (work_dir, workspace_dir, output_dir):
        path.mkdir(parents=True, exist_ok=False)
    process_environment = action_environment(environment, action_root, copilot_home)
    command = [
        executable,
        "eval",
        "--eval-spec",
        action["evalSpecPath"],
        "--work-dir",
        str(work_dir),
        "--workspace",
        str(workspace_dir),
        "--output-dir",
        str(output_dir),
        "--workers",
        "1",
        "--runs",
        str(action["runs"]),
    ]
    if model:
        command.extend(["--model", model])
    if judge_model:
        command.extend(["--judge-model", judge_model])
    if action["arm"] == "treatment":
        skill_path = Path(action["skillPath"]).resolve()
        if is_relative_to(skill_path, repository):
            raise ValueError("treatment skill must remain outside the target repository")
        skill_dir = skill_path if skill_path.is_dir() else skill_path.parent
        command.extend(["--skill-dir", str(skill_dir)])
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        env=process_environment,
        timeout=timeout_seconds,
    )
    (action_root / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (action_root / "stderr.log").write_text(result.stderr, encoding="utf-8")
    results_path = find_results_jsonl(output_dir)
    normalized = normalize(
        load_trial_records(results_path),
        action["caseIds"],
        action["runs"],
    )
    normalized["provenance"] = {
        "actionId": action_id,
        "arm": action["arm"],
        "split": action["split"],
        "model": model,
        "judgeModel": judge_model,
        "vallyExitCode": result.returncode,
        "resultsPath": str(results_path),
    }
    return normalized


def revision_version(skill_path: Path, proposal_key: str) -> str:
    root = skill_path if skill_path.is_dir() else skill_path.parent
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "content": path.read_text(encoding="utf-8"),
            }
        )
    if not files:
        raise ValueError("revised skill directory contains no files")
    return f"eval-{stable_hash([proposal_key, files], length=24)}"


def run_revision(
    action: dict[str, Any],
    *,
    proposal_key: str,
    command_template: str,
    run_dir: Path,
    timeout_seconds: int,
    environment: dict[str, str],
) -> tuple[str, str]:
    revision_dir = run_dir / "revisions" / f"iteration-{action['iteration']}"
    revision_dir.mkdir(parents=True, exist_ok=False)
    action_path = revision_dir / "revision-action.json"
    result_path = revision_dir / "revision-result.json"
    write_json(action_path, action)
    replacements = {
        "action": shlex.quote(str(action_path)),
        "output": shlex.quote(str(result_path)),
        "current_skill": shlex.quote(str(action["currentSkillPath"])),
        "iteration": str(action["iteration"]),
        "revision_dir": shlex.quote(str(revision_dir)),
    }
    try:
        rendered = command_template.format(**replacements)
    except KeyError as error:
        raise ValueError(f"unknown revision command placeholder: {error}") from error
    command = shlex.split(rendered)
    if not command:
        raise ValueError("revision command is empty")
    command[0] = resolve_executable(command[0])
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        env=environment,
        timeout=timeout_seconds,
    )
    (revision_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
    (revision_dir / "stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise ValueError(f"revision command exited {result.returncode}")
    document = read_json(result_path)
    if not isinstance(document, dict) or not isinstance(document.get("skillPath"), str):
        raise ValueError("revision result must contain skillPath")
    skill_path = Path(document["skillPath"]).resolve()
    if not skill_path.is_file() or skill_path.name != "SKILL.md":
        raise ValueError("revision skillPath must identify an existing SKILL.md")
    if not is_relative_to(skill_path, revision_dir.resolve()):
        raise ValueError("revised skill must be written inside the revision directory")
    validator = SCRIPT_DIR / "validate-skill.py"
    validation = subprocess.run(
        [sys.executable, str(validator), str(skill_path), "--json"],
        capture_output=True,
        check=False,
        text=True,
        env=environment,
        timeout=60,
    )
    (revision_dir / "validation.json").write_text(
        validation.stdout or validation.stderr,
        encoding="utf-8",
    )
    if validation.returncode != 0:
        raise ValueError("revised skill failed structural validation")
    version = revision_version(
        skill_path,
        proposal_key,
    )
    return str(skill_path), version


def block_state(state: dict[str, Any], reason: str) -> None:
    state["phase"] = None
    state["status"] = "blocked"
    state["decision"] = reason


def update_proposal(
    proposal_path: Path,
    state: dict[str, Any],
    evaluation_summary: dict[str, Any],
) -> None:
    proposal = read_json(proposal_path)
    if not isinstance(proposal, dict):
        raise ValueError("proposal must be a JSON object")
    proposal["evaluation"] = evaluation_summary
    if evaluation_summary.get("accepted") is True:
        revision = controller.current_revision(state)
        proposal["proposalVersion"] = revision["proposalVersion"]
        proposal["skillPath"] = revision["skillPath"]
    write_json(proposal_path, proposal)


def run_loop(args: argparse.Namespace) -> dict[str, Any]:
    state_path = args.state.resolve()
    state = controller.load_state(state_path)
    repository = args.repository.resolve()
    workspace_root = args.workspace_root.resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    environment = safe_environment(set(args.allow_env))
    version: str | None = None
    try:
        if not args.allow_host_execution:
            raise ValueError(
                "Vally local backend requires explicit --allow-host-execution"
            )
        if not repository.is_dir():
            raise ValueError(f"repository does not exist: {repository}")
        if is_relative_to(workspace_root, repository):
            raise ValueError("evaluation workspace root must be outside the repository")
        if is_relative_to(Path(state["runDir"]).resolve(), repository):
            raise ValueError("evaluation run directory must be outside the repository")
        executable = resolve_executable(args.vally_cli)
        version = check_vally_version(executable, args.vally_version, environment)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        block_state(state, f"evaluation_preflight_failed:{error}")
        write_json(state_path, state)
        output = controller.summary(state)
        output["vallyVersion"] = version
        output["actionsRun"] = 0
        write_json(args.summary, output)
        if args.proposal:
            update_proposal(args.proposal.resolve(), state, output)
        return output
    actions_run = 0
    while state.get("status") == "running":
        if actions_run >= args.max_actions:
            block_state(state, "evaluation_action_budget_exhausted")
            break
        action = controller.next_action(state)
        if action is None:
            block_state(state, "evaluation_controller_returned_no_action")
            break
        try:
            if action["kind"] == "run-vally":
                normalized = run_vally(
                    action,
                    executable=executable,
                    repository=repository,
                    workspace_root=workspace_root,
                    model=args.model,
                    judge_model=args.judge_model,
                    timeout_seconds=args.timeout_seconds,
                    environment=environment,
                    copilot_home=args.copilot_home,
                )
                normalized_path = Path(action["normalizedResultPath"])
                write_json(normalized_path, normalized)
                controller.record_result(
                    state,
                    action["split"],
                    action["arm"],
                    normalized,
                )
            elif action["kind"] == "revise-proposal":
                if not args.revision_command:
                    block_state(state, "revision_command_unavailable")
                    break
                skill_path, version_value = run_revision(
                    action,
                    proposal_key=state["proposal"]["proposalKey"],
                    command_template=args.revision_command,
                    run_dir=Path(state["runDir"]),
                    timeout_seconds=args.timeout_seconds,
                    environment=environment,
                )
                controller.record_revision(state, skill_path, version_value)
            else:
                block_state(state, f"unsupported_evaluation_action:{action['kind']}")
                break
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            block_state(state, f"evaluation_action_failed:{action['actionId']}:{error}")
            break
        finally:
            actions_run += 1
            write_json(state_path, state)
    output = controller.summary(state)
    output["vallyVersion"] = version
    output["actionsRun"] = actions_run
    write_json(args.summary, output)
    if args.proposal:
        update_proposal(args.proposal.resolve(), state, output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--vally-cli", default="vally")
    parser.add_argument("--vally-version", default="0.14.0")
    parser.add_argument("--model")
    parser.add_argument("--judge-model")
    parser.add_argument("--revision-command")
    parser.add_argument("--copilot-home", type=Path)
    parser.add_argument("--allow-host-execution", action="store_true")
    parser.add_argument("--allow-env", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--max-actions", type=int, default=16)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = run_loop(args)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"error: {error}") from error
    if result["status"] != "accepted":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
