# Copilot coding agent completion guard

The Copilot coding agent (CCA) can run an opt-in completion predicate at
`onAgentStop` and after `session.idle`. This document is the exact contract that
`repository-skill-forge` implements so a run can never return a normal final
response while extraction is still non-terminal.

CCA owns generic diagnostics: turns, tool calls, finish and stop reason, context
usage and headroom, and compaction. Everything Forge-specific comes from the
predicate reason and the run marker snapshot described below.

## Predicate contract

The predicate writes exactly one JSON object as the final line on stdout:

```json
{"status":"complete"}
```

```json
{"status":"incomplete","reason":"...","continuePrompt":"..."}
```

Exit status is the fallback signal: `0` for complete, `1` for incomplete. The
full diagnostic snapshot, including the marker summary and controller counters,
is written to stderr as a single JSON line. Pass `--quiet` to suppress it.

`reason` and `continuePrompt` are bounded (`--max-reason-chars`, default 1200;
`--max-prompt-chars`, default 2400). Pending action IDs appear early in both
strings, and blocker fields are reproduced verbatim, so truncation never removes
the actionable detail.

## Stable command

`run-marker.py init` writes an executable launcher next to the marker. The
launcher hard-codes the interpreter, the predicate path inside the installed
plugin, and the marker path, so the configured command needs no knowledge of
where the plugin was installed:

```
/tmp/copilot-skill-forge/completion-predicate
```

Use this form when a repository may also run sessions that never invoke Skill
Forge, because the launcher does not exist until a Forge run initialises it:

```sh
sh -c 'if [ -x /tmp/copilot-skill-forge/completion-predicate ]; then exec /tmp/copilot-skill-forge/completion-predicate; else printf "%s\n" "{\"status\":\"complete\"}"; fi'
```

Add `--require-marker` for repositories where every agent session is a Forge
run. It turns a missing marker into an `incomplete` verdict instead of a pass.

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `FORGE_RUN_MARKER` | Absolute path to the marker file, for a direct predicate or `run-marker.py` invocation. | `$FORGE_MARKER_DIR/run-marker.json` |
| `FORGE_MARKER_DIR` | Directory holding the marker and launcher. | `/tmp/copilot-skill-forge` |

Command-line flags win over both: `--marker` overrides `FORGE_RUN_MARKER`, and
`--marker-dir` overrides `FORGE_MARKER_DIR`.

Every marker location must be an **absolute** path. A relative value is refused
with a clear error rather than resolved, because it would otherwise anchor to
the working directory, and the run and the agent stop hook do not share one.
Silently resolving it would point the guard at a path that does not exist, which
the permissive wrapper would report as `complete`.

The generated launcher **pins** the marker path of the run that created it and
does not honour an inherited `FORGE_RUN_MARKER`. A stale value left over from an
earlier run, a container image default, or a co-scheduled job would otherwise
retarget the guard at another run's state. Use the environment variables or the
flags to point a *direct* invocation somewhere else; the launcher always speaks
for its own run.

The marker directory is created mode `0700` and is re-checked on every use. A
directory that is a symlink, is owned by another user, or is group or world
writable is refused, so a co-tenant on a shared host cannot plant a marker or
replace the launcher. On such a host, point `FORGE_MARKER_DIR` at a private
directory instead. The predicate applies the same check, but only once a marker
file actually exists, so a session that never runs Skill Forge is never blocked.

Both scripts disable bytecode caching, and the generated launcher exports
`PYTHONDONTWRITEBYTECODE=1`, so running the guard never writes into the
installed plugin directory.

## Verdicts

| Verdict | Status | Trigger |
| --- | --- | --- |
| `extraction-terminal` | complete | `assert-terminal` succeeds with `complete` or `partial` |
| `no-active-run` | complete | No marker, and `--require-marker` was not set |
| `extraction-running` | incomplete | Controller status is `running` |
| `extraction-blocked` | incomplete | Controller status is terminal `blocked` |
| `state-inconsistent` | incomplete | Controller invariants fail or the snapshot cannot be built |
| `state-unsupported` | incomplete | Controller status is not a recognised value |
| `state-missing` | incomplete | The marker points at a state file that does not exist |
| `state-unreadable` | incomplete | The state file is not a readable JSON object |
| `marker-stale` | incomplete | The state is gone and the marker was not refreshed within `--max-age-seconds` |
| `marker-unreadable` | incomplete | The marker is malformed or has an unsupported schema |
| `marker-unresolvable` | incomplete | The configured marker location is not a usable absolute path |
| `marker-untrusted` | incomplete | The marker directory is a symlink, is not owned by this user, or is group or world writable |
| `marker-missing` | incomplete | No marker, and `--require-marker` was set |
| `predicate-error` | incomplete | The predicate itself failed before it could judge the run |

The guard fails closed. `blocked` is terminal for the controller but is never a
success for the guard, because a blocked run must produce an explicit `BLOCKED`
report rather than a normal final response. The `continuePrompt` for `blocked`
tells the agent to report, not to resume extraction. CCA is responsible for
bounding continuation attempts.

## Run lifecycle

The predicate is deterministic and side-effect free. All writes happen in
`run-marker.py`, which is the only component that touches the marker.

| Point in the run | Command |
| --- | --- |
| After `extraction-controller.py init` | `run-marker.py init --state $RUN_DIR/extraction-state.json --checkpoint $RUN_DIR/checkpoint-summary.json` |
| After each recorded action batch and checkpoint | `run-marker.py refresh` |
| After `assert-terminal` succeeds | `run-marker.py finish` |
| Before discarding `$RUN_DIR` | `run-marker.py clear` |

`clear` refuses to run while the marker phase is still `active`, so the marker
cannot be removed before terminal assertion. There is deliberately no override
flag: an escape hatch that removed an active marker would let the permissive
launcher report `complete` mid-run, which is the exact failure this guard
prevents. `clear` leaves the launcher in place so the configured command keeps
returning `{"status":"complete"}` afterwards. Pass `--purge` to remove the
launcher too, which is also refused while the marker is active.

One marker location owns one run. `init` refuses to displace a marker that is
still `active` and belongs to a different run, so a second concurrent run cannot
silently retarget the first run's stop hook at its own state. Re-running `init`
for the same run is idempotent. If two Forge runs really must share a container,
give each its own location with `--marker-dir` or `FORGE_MARKER_DIR`.

`refresh` and `finish` locate the controller state through the marker, not
through model memory, and rewrite the marker atomically with an incremented
`revision`. Both validate the marker directory before reading anything inside
it. Marker locations must be absolute and are normalized without resolving
symlinks, so the guard reaches the same marker whatever working directory it is
invoked from, while the symlink rejection above still applies to the final path
component.

## Snapshot fields

`extraction-controller.py diagnostics --state ... [--checkpoint ...]` emits the
same snapshot that the marker embeds. It is derived entirely from existing
controller state; it is not a second state machine.

- `status`, `terminal`, `consistent`, `invariantError`
- `counters`: query attempts, successful and failed queries, rows, tool calls,
  artifact bytes
- `actions`: issued and pending action IDs with counts, handled count, retries
- `sessions`: discovered, completed, omitted units and their kinds
- `batches`: total, complete, omitted, pending, and a per-status breakdown
- `partitions`: total, discovery complete, pending discovery, omitted
- `coverage`: session coverage and its known or unknown status, fallback count
- `checkpoint`: the latest `checkpoint-completed-batches.py` summary
- `blocker` and `blockerCount`

## Testing this plugin branch from a cloud agent run

`github/copilot-dreams` selects this plugin in `.github/copilot/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "repo-dreaming": {
      "source": { "source": "github", "repo": "dsp-testing/SkillForge_test" }
    }
  },
  "enabledPlugins": { "repo-dreamer@repo-dreaming": true }
}
```

Two facts are documented and verified:

- The coding agent, not only the CLI, reads `enabledPlugins` and
  `extraKnownMarketplaces` from the repository settings file.
- `extraKnownMarketplaces` is honoured at repository scope, and repository
  entries override user entries for the same key.

To point a cloud agent run at an unmerged branch, push the branch to
`dsp-testing/SkillForge_test` and add a ref to the marketplace source:

```json
{
  "extraKnownMarketplaces": {
    "repo-dreaming": {
      "source": {
        "source": "github",
        "repo": "dsp-testing/SkillForge_test",
        "ref": "morabbin-forge-completion-guard"
      }
    }
  },
  "enabledPlugins": { "repo-dreamer@repo-dreaming": true }
}
```

Caveat: `ref` and `sha` are documented on a plugin entry's `source` object, and
`copilot plugin marketplace add` accepts `owner/repo#ref`, but the published
schema for an `extraKnownMarketplaces` entry leaves the value shape unexpanded
and never names `ref` or `sha`. Treat the snippet above as unverified until a
run proves it resolves the branch. Confirm resolution from the run itself, for
example by checking that `reference/cca-completion-guard.md` and
`scripts/completion-predicate.py` exist in the loaded skill directory.

If the ref is ignored, fall back in this order:

1. String source form with a ref suffix:
   `"source": "dsp-testing/SkillForge_test#morabbin-forge-completion-guard"`.
2. A disposable branch in `dsp-testing/SkillForge_test` carrying these commits,
   referenced the same way, so the test branch is obvious and easy to delete.
3. A temporary fork with the branch merged to its default branch, referenced
   without a ref. This uses only the documented default-branch path.

Do not use a setup-steps clone into `COPILOT_SKILLS_DIRS` for this test. It
loads the skill outside the plugin, so it would not exercise the plugin
resolution path the scheduled job actually uses.
