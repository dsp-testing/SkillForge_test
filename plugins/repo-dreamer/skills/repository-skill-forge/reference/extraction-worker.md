# Extraction worker protocol

`scripts/extraction-worker.py` moves every deterministic extraction step out of
repeated model turns. The model executes `session_store_sql` calls and makes
proposal decisions; the worker owns everything else.

## Commands

| Command | Purpose |
| --- | --- |
| `start --run-dir --repository --window-end` | Initialize controller state, worker state, and emit the first wave. |
| `advance --run-dir` | Harvest the issued wave, record every outcome, checkpoint, emit the next wave or the terminal result. |
| `status --run-dir [--assert-terminal]` | Read-only progress and terminal assertion. |

`start` accepts every controller limit (`--discovery-page-size`,
`--session-batch-size`, `--tool-page-size`, `--max-rows`,
`--max-artifact-bytes`, `--min-window-minutes`, `--max-concurrent-batches`,
`--max-query-retries`, `--enable-tool-event-fallback`, `--allow-partial` /
`--fail-on-omission`) plus `--window-hours`, `--window-start`, `--run-id`, and
repeatable `--main-branch`. `advance` accepts `--wait` and `--poll-interval` to
block for in-flight tool results, and both accept `--events-root` and `--out`.

## What one `advance` does

1. Probes the current session event log for every action the controller has
   issued, matching on the exact submitted SQL and the action ID passed as
   `description`, and ignoring any call that had already completed when the
   wave was emitted.
2. Materializes each ready result, following runtime spill receipts, into the
   controller's `outputPath` artifact.
3. Records outcomes through the controller: successes first, then terminal
   failures, always in issued-action order rather than completion order.
4. Checkpoints every newly completed batch, converting checkpoint failure into
   a terminal controller blocker.
5. Generates the next bounded wave, records the tool calls already completed
   for each of its actions, then finalizes, re-checkpoints to attach terminal
   coverage, and asserts terminal status when no actions remain.

An unresolved action stays `pending`: nothing is recorded, the action stays
issued, and the identical query is re-emitted in the next wave. A timed-out
query is recorded as a failure and never re-issued identically.

A retriable failure is different: the controller re-issues the same action ID
and the same SQL. The wave boundary recorded in step 5 is what keeps that
retry honest, because without it the next `advance` would match the previous
attempt's completed call and consume the retry before the new tool call
finished.

## Outcome mapping

| Event-log observation | Recorded as |
| --- | --- |
| A call that completed before this wave was emitted | ignored entirely |
| No completed matching call | `pending`, nothing recorded |
| `success: false` | `record-failure --error-kind auto` with the tool's own error text |
| Action ID matched, submitted SQL differed | `record-failure --error-kind handoff` |
| Missing or unreadable spill file, unparsable table, or rejected result | `record-artifact-failure` |
| Row or artifact limit exceeded | `record-failure` without retry |
| Parsed rows written | `record-success` |

Passing the tool's own error text through `--error-kind auto` is what lets the
controller classify timeouts, rate limits, and server errors correctly.

## Envelope

Every command prints one `workerEnvelope` (`assets/schemas.json`,
`protocolVersion: 1`). It carries bounded counters, per-action outcomes, a
checkpoint summary, and, for a wave, exactly the arguments needed for each
`session_store_sql` call. Session ID lists, result rows, omission session
hashes, fallback history, and controller internals stay in run-local files:

- `$RUN_DIR/extraction-state.json`: controller state, the only extraction writer;
- `$RUN_DIR/worker/worker-state.json`: run identity, cycle counters, and the
  per-action wave boundary of already-completed tool calls;
- `$RUN_DIR/worker/wave.json`: full action manifest, materializer compatible;
- `$RUN_DIR/worker/envelope.json`: last emitted envelope;
- `$RUN_DIR/worker/coverage.json`: complete terminal coverage with omissions;
- `$RUN_DIR/primitives.sanitized.json`: promoted sanitized ledger.

## Measured reduction

`tests/test_worker_cycle_simulation.py` replays the historical 286-session,
12-batch shape with `sessionBatchSize=25` and `maxConcurrentBatches=3`. It is a
deterministic fixture: no network and no external state.

| Metric | Before | After | Change |
| --- | --- | --- | --- |
| Orchestration shell commands | 184 | 19 | 89.7% fewer |
| Model turns | 53 | 35 | 34.0% fewer |
| Model turns with the wave fused via `advance --wait` | 53 | 18 | 66.0% fewer |
| Orchestration transcript bytes | 123,064 | 81,080 | 34.1% fewer |
| `session_store_sql` calls and results | 49 | 49 | unchanged |

"Before" reconstructs the exact per-wave command sequence the previous policy
required: one `extraction-controller.py next`, then per action one
`materialize-session-query.py`, one `record-success`, and one `mv`, then one
`checkpoint-completed-batches.py`, plus the full action manifest the model had
to read to obtain each query. Both sides exclude the `session_store_sql` calls
and their results, which are identical.

The 35-turn figure is the guaranteed one: one turn issues a wave's tool calls
and the next runs `advance`. The 18-turn figure additionally assumes the
runtime records completed tool calls in the event log while a concurrently
issued `advance --wait` is still running. That is a deployment property, not a
guarantee, so `--wait` is an optimization: on timeout every unresolved action
is reported `pending`, nothing is recorded, and the run degrades to the
two-turn pattern with no lost or duplicated work.

## Remaining runtime boundary

`session_store_sql` executes inside the agent runtime. Nothing in this
repository can invoke it from a plain Python process, so the tool wave is the
one step the worker cannot absorb.

Evidence:

- the worker's only supported transport is the runtime's own completed-call
  record in `~/.copilot/session-state/<session>/events.jsonl`, which exists only
  after the runtime executes the call;
- the local store at `~/.copilot/session-store.db` exposes `sessions`, `turns`,
  `checkpoints`, `session_files`, `session_refs`, `assistant_usage_events`, and
  `search_index`, and has no `tool_requests` or `events` table, so it cannot
  serve the primary strategy even for this machine's own sessions;
- the local store holds only personal sessions, never the repository-scoped
  window this skill requires;
- no packaged command-line entry point for the repository-scoped store is
  documented, and probing private endpoints is out of scope.

### Minimal cross-repo follow-up contract

A runtime-side executor would close the loop without changing anything here.
The smallest sufficient interface is a callable that accepts the existing wave
manifest and writes the existing artifacts:

```
execute(waveManifestPath) -> [
  {
    "actionId": str,
    "status": "success" | "failure",
    "outputPath": str,   # JSON array of row objects, written on success
    "error": str         # verbatim tool error text, on failure
  }
]
```

Requirements:

- honour `description` as the exact submitted action ID and `sql` verbatim, so
  handoff verification keeps working;
- execute the actions in one manifest concurrently, never more than the
  manifest contains;
- return the tool's verbatim error text so controller error classification and
  the timeout invariants are preserved;
- write results to each action's `outputPath` rather than returning rows.

Given that callable, `advance` gains a `--execute` mode that replaces the
event-log probe, and a full run collapses from 18 model turns to 1. Until then,
the model issues the tool calls and the worker does everything else.

## Fallback

The per-action commands remain supported for a bounded recovery path when the
worker cannot run: `extraction-controller.py next --parallel --out`,
`materialize-session-query.py --actions --action`, `record-success`,
`record-failure`, `record-artifact-failure`, `record-checkpoint-failure`,
`checkpoint-completed-batches.py`, and `assert-terminal`. They read and write
the same run-local files, so a run may switch between the two paths.
