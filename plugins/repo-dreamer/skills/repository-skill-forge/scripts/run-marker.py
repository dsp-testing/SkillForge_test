#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation. All rights reserved.

"""Maintain the run-local Skill Forge completion marker and predicate launcher."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import forge_marker
from forge_common import read_json, write_json

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
PREDICATE_PATH = SCRIPT_DIR / "completion-predicate.py"


def load_controller() -> Any:
    spec = importlib.util.spec_from_file_location(
        "forge_extraction_controller",
        SCRIPT_DIR / "extraction-controller.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load extraction-controller.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller = load_controller()


def read_state(path: str | Path) -> dict[str, Any]:
    state = read_json(path)
    if not isinstance(state, dict):
        raise ValueError("extraction state must be a JSON object")
    return state


def read_checkpoint(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    checkpoint_path = Path(path)
    if not checkpoint_path.is_file():
        return None
    checkpoint = read_json(checkpoint_path)
    if not isinstance(checkpoint, dict):
        raise ValueError("checkpoint summary must be a JSON object")
    return checkpoint


def snapshot_for(marker: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return controller.diagnostics(
        state,
        checkpoint=read_checkpoint(marker.get("checkpointPath")),
    )


def emit(payload: Any, out: str | None) -> None:
    if out:
        write_json(out, payload)
    else:
        print(json.dumps(payload, indent=2))


def load_marker(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    """Reject an untrusted marker directory before reading anything it contains."""
    marker_path = forge_marker.resolve_marker_path(args.marker, args.marker_dir)
    forge_marker.assert_private_directory(marker_path.parent)
    return marker_path, forge_marker.read_marker(marker_path)


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    state_path = forge_marker.absolute(args.state)
    state = read_state(state_path)
    marker_path = forge_marker.resolve_marker_path(args.marker, args.marker_dir)
    marker = forge_marker.build_marker(
        state=state,
        state_path=state_path,
        run_id=args.run_id,
        checkpoint_path=args.checkpoint,
        ledger_path=args.ledger,
        skill_dir=SKILL_DIR,
        predicate_path=PREDICATE_PATH,
    )
    reject_foreign_active_run(marker_path, marker["runId"])
    marker["snapshot"] = snapshot_for(marker, state)
    forge_marker.write_marker(marker_path, marker)
    launcher = forge_marker.install_launcher(
        launcher_path=marker_path.parent / forge_marker.LAUNCHER_FILENAME,
        marker_path=marker_path,
        predicate_path=PREDICATE_PATH,
        python_executable=sys.executable or "python3",
    )
    return {
        "markerPath": str(marker_path),
        "launcherPath": str(launcher),
        "marker": marker,
    }


def reject_foreign_active_run(marker_path: Path, run_id: str) -> None:
    """One marker location owns one run, so never displace another active run."""
    if not os.path.lexists(marker_path):
        return
    forge_marker.assert_private_directory(marker_path.parent)
    try:
        existing = forge_marker.read_marker(marker_path)
    except forge_marker.MarkerError:
        return
    if (
        existing.get("phase") == forge_marker.ACTIVE_PHASE
        and existing.get("runId") != run_id
    ):
        raise ValueError(
            f"run marker {marker_path} already belongs to active run "
            f"{existing.get('runId')}; finish that run, or give this run its own "
            f"location with --marker-dir or {forge_marker.MARKER_DIR_ENV}"
        )


def command_refresh(args: argparse.Namespace) -> dict[str, Any]:
    marker_path, marker = load_marker(args)
    state = read_state(marker["statePath"])
    updated = forge_marker.advance_marker(
        marker,
        checkpoint_path=args.checkpoint,
        ledger_path=args.ledger,
    )
    updated["snapshot"] = snapshot_for(updated, state)
    forge_marker.write_marker(marker_path, updated)
    return {"markerPath": str(marker_path), "marker": updated}


def command_finish(args: argparse.Namespace) -> dict[str, Any]:
    marker_path, marker = load_marker(args)
    state = read_state(marker["statePath"])
    summary = controller.terminal_summary(state)
    updated = forge_marker.advance_marker(
        marker,
        phase=forge_marker.TERMINAL_PHASE,
        terminal_summary=summary,
        checkpoint_path=args.checkpoint,
        ledger_path=args.ledger,
    )
    updated["snapshot"] = snapshot_for(updated, state)
    forge_marker.write_marker(marker_path, updated)
    return {"markerPath": str(marker_path), "marker": updated}


def command_clear(args: argparse.Namespace) -> dict[str, Any]:
    marker_path = forge_marker.resolve_marker_path(args.marker, args.marker_dir)
    launcher_path = marker_path.parent / forge_marker.LAUNCHER_FILENAME
    if os.path.lexists(marker_path):
        forge_marker.assert_private_directory(marker_path.parent)
        marker = forge_marker.read_marker(marker_path)
        if marker.get("phase") != forge_marker.TERMINAL_PHASE:
            raise ValueError(
                "refusing to clear an active run marker before terminal assertion; "
                "run `run-marker.py finish` after `assert-terminal` succeeds"
            )
        marker_path.unlink()
        cleared = True
    else:
        cleared = False
    launcher_removed = False
    if args.purge and launcher_path.exists():
        launcher_path.unlink()
        launcher_removed = True
    return {
        "markerPath": str(marker_path),
        "cleared": cleared,
        "launcherRemoved": launcher_removed,
    }


def command_show(args: argparse.Namespace) -> dict[str, Any]:
    marker_path, marker = load_marker(args)
    return {"markerPath": str(marker_path), "marker": marker}


def add_marker_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--marker")
    parser.add_argument("--marker-dir")
    parser.add_argument("--out")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init")
    init.add_argument("--state", required=True)
    init.add_argument("--run-id")
    init.add_argument("--checkpoint")
    init.add_argument("--ledger")
    add_marker_arguments(init)

    refresh = subparsers.add_parser("refresh")
    refresh.add_argument("--checkpoint")
    refresh.add_argument("--ledger")
    add_marker_arguments(refresh)

    finish = subparsers.add_parser("finish")
    finish.add_argument("--checkpoint")
    finish.add_argument("--ledger")
    add_marker_arguments(finish)

    clear = subparsers.add_parser("clear")
    clear.add_argument("--purge", action="store_true")
    add_marker_arguments(clear)

    show = subparsers.add_parser("show")
    add_marker_arguments(show)

    args = parser.parse_args()
    commands = {
        "init": command_init,
        "refresh": command_refresh,
        "finish": command_finish,
        "clear": command_clear,
        "show": command_show,
    }
    try:
        emit(commands[args.command](args), args.out)
    except FileNotFoundError as error:
        raise SystemExit(f"run marker or extraction state not found: {error}") from error
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
