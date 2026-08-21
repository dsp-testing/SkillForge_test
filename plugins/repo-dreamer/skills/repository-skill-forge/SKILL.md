---
name: repository-skill-forge
description: Analyze a fixed repository session window and publish at most one PR-reconciled skill proposal without durable issue state.
user-invocable: true
---

# Repository Skill Forge

Mine a fixed repository-scoped session window with the deployed fast query
strategy, derive proposals from only that run's evidence, and use machine-
readable metadata in open and closed Forge PRs for cross-run deduplication.

## Conditions

Use this skill only when:

- `session_store_sql` supports repository-scoped queries for the target;
- Python 3.10 or later is available;
- GitHub reads and writes use approved GitHub MCP tools;
- the run has an isolated scratch directory outside the checkout.

The workflow does not have trusted actor identity. User diversity remains
explicitly unknown.

## Interface

Inputs:

- `repository`: exact `owner/name`;
- `runId`, `runDir`, and exclusive UTC `windowEnd`;
- `windowHours`: default `96` (four days);
- `discoveryPageSize`: default `500`;
- `sessionBatchSize`: default `25`;
- `toolPageSize`: default `1000`;
- `maxRows`: default `1000`;
- `maxArtifactBytes`: default `10000000`;
- `maxQueryRetries`: default `1` retry for non-timeout transient failures;
- `minWindowMinutes`: default `15`;
- `maxConcurrentBatches`: default `3`;
- `enableToolEventFallback`: default `false`;
- `allowPartial`: default `true`.

There is no issue-backed state, host state path, cursor, observation ledger,
proposal queue, or proposal history. Set `windowStart = windowEnd - windowHours`
on every run. A missed run creates an evidence gap; do not silently widen the
window or reconstruct prior evidence.

Run-local extraction state under `runDir` is required for resumability. It is
ephemeral execution state, not cross-run Forge memory.

A run-local completion marker under `$FORGE_MARKER_DIR` (default
`/tmp/copilot-skill-forge`) lets an opt-in Copilot coding agent completion guard
verify terminal status without model memory. It is process-local, never written
into the checkout, and never carries evidence across runs. See
`reference/cca-completion-guard.md` for the command and configuration contract.

## Policy

### 1. Establish the fixed window and PR catalog

Use the exact fixed rolling window for every run. Do not derive it from an
issue, a previous run, or the last published PR.

Search open and closed pull requests for the literal
`repository-skill-forge-proposal:v1` marker, regardless of labels. Request at
least PR number, URL, state, draft status, merged timestamp, updated timestamp,
and complete body for every match. Also use general PR search when evaluating a
candidate to find semantically related work that predates the marker or was not
created by Forge. Labels are optional organizational metadata and are never an
identity, discovery, deduplication, or blocking mechanism.

Write the returned PR array to `$RUN_DIR/forge-prs.json`, then build the catalog:

```bash
python3 "$SKILL_DIR/scripts/proposal-ledger.py" catalog \
  --prs "$RUN_DIR/forge-prs.json" \
  --out "$RUN_DIR/proposal-catalog.json"
```

Only PRs containing one valid `repository-skill-forge-proposal:v1` marker enter
the catalog. A malformed or duplicate marker is an explicit integration error;
never guess its identity.

Read default-branch repository content through GitHub MCP by omitting the
optional `ref`. Exact target branch and SHA are needed only if a proposal is
selected for publication.

### 2. Run the deployed fast extraction strategy

`extraction-worker.py` owns the extraction loop. It generates bounded actions,
materializes exact results, records every outcome through
`extraction-controller.py`, checkpoints completed batches, and asserts terminal
status. Execute only the `session_store_sql` calls it asks for.

```bash
python3 "$SKILL_DIR/scripts/extraction-worker.py" start \
  --run-dir "$RUN_DIR" \
  --repository "$REPOSITORY" \
  --window-end "$WINDOW_END" \
  --window-hours 96 \
  --main-branch "$DEFAULT_BRANCH"
```

Immediately after `start` creates run-local state, publish the run marker so
completion can be verified from state rather than from memory:

```bash
python3 "$SKILL_DIR/scripts/run-marker.py" init \
  --state "$RUN_DIR/extraction-state.json" \
  --checkpoint "$RUN_DIR/checkpoint-summary.json" \
  --ledger "$RUN_DIR/primitives.sanitized.json"
```

Every command prints one `workerEnvelope` (`assets/schemas.json`). While `kind`
is `wave`, call `session_store_sql` once per entry in `wave.actions`, passing
that entry's `description` and `query` unchanged, executing the wave's calls
concurrently, then run:

```bash
python3 "$SKILL_DIR/scripts/extraction-worker.py" advance --run-dir "$RUN_DIR"
```

Refresh the run marker immediately after every `advance` so its diagnostic
snapshot always reflects the persisted controller and checkpoint state:

```bash
python3 "$SKILL_DIR/scripts/run-marker.py" refresh
```

Repeat until `kind` is `terminal`. Optionally add `--wait 120` to `advance` to
issue it in the same turn as its wave; an unresolved action is reported
`pending`, nothing is recorded, and the identical query is re-emitted, so a
timeout degrades safely to the two-turn pattern.

Never edit worker or controller SQL, never call `session_store_sql` outside a
wave, and never retry or omit a query yourself. The worker records every
outcome exactly once and the controller is the only writer of run-local
extraction state.

The primary strategy is:

1. select sessions updated inside the fixed window, returning only `id` and
   `updated_at`, ordered by `(updated_at, id)` for keyset pagination, with an
   exact SQL repository predicate in addition to mandatory tool-level scope;
2. fetch exact-ID `sessions` metadata in batches of 25;
3. fetch bounded `session_refs` and `session_files`;
4. fetch relevant `tool_requests` directly with pages of 1000. Preserve
   tool-level `exit_code` and `completed_at` artifact columns as `NULL` until
   Session Search exposes materialized completion metadata; do not join the
   primary query to `events`.

Tool requests are selected by exact session ID. A session created before the
window but updated inside it must not be excluded. Keep `sessionBatchSize` at
25 unless a measured deployment limit requires a smaller value.

The materialized `sessions.updated_at` value is the session's latest event
timestamp and is the completion-time proxy. Primitive derivation uses it when
a tool-level timestamp is unavailable. Tool outcomes remain `unknown` without
an explicit exit code; do not infer success from a tool request's presence.

Do not fetch turns, assistant messages, or unrelated tools. Do not run a
separate repository-wide count query. Discovery is authoritative, and zero
sessions is a successful no-evidence result.

Every primary discovery query requests `discoveryPageSize + 1` rows. The
controller accepts the first page-size rows and continues after the last
`(updated_at, id)` pair, and processes every successful page before requesting
the next. Discovery manifests contain one action; post-discovery manifests
contain up to `maxConcurrentBatches` actions from distinct session batches.
Completed discovery partitions are extracted before unresolved partitions so
slow windows do not starve analysis of evidence already found.

Only non-timeout transient network, rate-limit, or server failures are retried,
at most once. An identical timed-out query is never repeated. Primary discovery
timeout before the first successful page uses adaptive time splitting down to
`minWindowMinutes`. A timeout after successful pages discloses the uncollected
remainder without discarding accepted sessions.

The worker checkpoints after recording each wave. Checkpointing is idempotent by
batch ID, atomically promotes the sanitized run ledger before deleting resolved
raw artifacts, and attaches final extraction coverage once the controller is
terminal. A checkpoint failure becomes a terminal controller blocker so it can
never be mistaken for unfinished `running` extraction.

Each checkpoint is also written to `$RUN_DIR/checkpoint-summary.json`, so the
run marker's diagnostic snapshot reports current checkpoint coverage rather than
model recollection.

Immediately before any final response, publication decision, or run-directory
cleanup, require:

```bash
python3 "$SKILL_DIR/scripts/extraction-worker.py" status \
  --run-dir "$RUN_DIR" --assert-terminal
```

Only once that command succeeds, record the terminal summary in the marker:

```bash
python3 "$SKILL_DIR/scripts/run-marker.py" finish
```

Remove the marker with `run-marker.py clear` only when `$RUN_DIR` is discarded.
It refuses to clear a marker that has not reached the terminal phase, and has no
override flag.

If this fails because status is `running`, a final response and cleanup are
forbidden. Read `pendingActionIds` from its envelope, execute the outstanding
wave, and keep advancing until it succeeds.

A run is `BLOCKED` only when that assertion succeeds and reports controller
status `blocked`. Never translate `running`, pending work, elapsed time, action
volume, or an unrelated helper-shell/display error into `BLOCKED`. Fix or omit
nonessential display commands and continue from the persisted run-local state.
Do not delete `$RUN_DIR` while status is `running`.

Once the worker is started, do not stop for label, target-identity, or
publication-tool checks while its status remains `running`.

`reference/extraction-worker.md` documents the envelope contract, the exact
outcome mapping, the measured cycle reduction, the remaining runtime boundary,
and the bounded per-action fallback commands for when the worker cannot run.

### 3. Handle irreducible query failures

Tool-event fallback is opt-in. With
`enableToolEventFallback=false`, timed-out post-discovery units are omitted
without an identical retry or page-size reduction when partial mode is enabled.

When enabled, fallback applies only to a failed tool unit and never queries
outside the fixed window. Exact-session metadata remains authoritative from
`sessions`; timed-out references and files are omitted rather than retried with
smaller limits.

With `allowPartial=true`, irreducible discovery failures are recorded by time
range and post-discovery failures by repository-salted session hashes. Use
`--fail-on-omission` for fail-closed behavior. Partial evidence must remain
explicit throughout proposal validation and publication.

### 4. Aggregate the progressively sanitized run

After terminal assertion, require `$RUN_DIR/primitives.sanitized.json`. The
worker has already promoted every completed batch and attached the controller's
complete or disclosed-partial coverage. Do not repeat normalization or
sanitization at the end of extraction.

Raw commands and source content remain ephemeral. `aggregate-primitives.py`
deduplicates only the current run and emits patterns and candidates; it does
not read or write durable Forge state:

```bash
python3 "$SKILL_DIR/scripts/aggregate-primitives.py" \
  --in "$RUN_DIR/primitives.sanitized.json" \
  --out "$RUN_DIR/candidates.json" \
  --repository "$REPOSITORY" \
  --as-of "$WINDOW_END" \
  --active-days 7 \
  --stale-days 7 \
  --allow-unknown-outcomes
```

Unknown-outcome mode bypasses outcome thresholds only for patterns with zero
known outcomes. If explicit outcomes exist, normal outcome gates apply.
Eligibility always requires at least three distinct sessions, two distinct
days, and either two merged PRs or two mainline-corroborated observations.
When outcomes are known, it also requires three known outcomes, 0.7 success,
and 0.5 scored coverage.

### 5. Author and independently review proposals

Cluster eligible candidates by repository subject. Assign a stable
`proposalKey`, include all cluster `candidateIds`, and derive
`proposalVersion` from the complete proposal evidence and generated content.
Assign deterministic non-negative `rank` values.

Validate and independently review every proposal. Compare it with repository
skills and the complete Forge PR catalog. Store each promoted proposal's marker
with that proposal rather than in a shared run-level file:

```bash
python3 "$SKILL_DIR/scripts/proposal-ledger.py" marker \
  --proposal "$PROPOSAL_JSON" \
  --out "$RUN_DIR/proposals/$PROPOSAL_KEY/proposal-marker.md"
```

The marker is persistent PR metadata. Do not edit or remove it when updating a
PR. It lets later stateless runs distinguish unchanged, revised, rejected,
open, and merged proposals without an issue ledger.

### 6. Reconcile and publish at most one proposal

Select from validated proposals against the complete PR catalog:

```bash
python3 "$SKILL_DIR/scripts/proposal-ledger.py" select \
  --proposals "$RUN_DIR/proposals.json" \
  --catalog "$RUN_DIR/proposal-catalog.json" \
  --out "$RUN_DIR/proposal-selection.json"
```

The selected entry contains `selection.marker`, generated directly from the
selected proposal. Pass that exact value unchanged in the PR body; never use a
shared marker file or a marker belonging to another proposal.

Reconciliation rules:

- same `proposalKey` and `proposalVersion` in any open, closed, or merged PR:
  skip as already represented;
- same key with a different version in an open PR: update that PR;
- same key with a different version only in closed-unmerged PRs: a materially
  revised proposal may create a replacement PR;
- same key in a merged PR: create only an
  `improve_existing_skill` or `merge_skills` proposal;
- `hold_as_pattern_only`: do not publish;
- select no more than one allowed create/update action by `(rank, proposalKey)`.

Only after selection, resolve the target branch and SHA. Prefer approved GitHub
MCP metadata. Otherwise use the checkout's local remote refs without a network
request:

```bash
REPOSITORY_DIR="$(git rev-parse --show-toplevel)"
DEFAULT_REF="$(
  git -C "$REPOSITORY_DIR" symbolic-ref --quiet refs/remotes/origin/HEAD
)"
TARGET_BRANCH="${DEFAULT_REF#refs/remotes/origin/}"
TARGET_SHA="$(
  git -C "$REPOSITORY_DIR" rev-parse --verify "$DEFAULT_REF^{commit}"
)"
```

If no proposal is selected, missing target identity must not block the run.
If selected and target identity cannot be resolved, block before publication.

For the selected proposal only, use `prompts/author-pr-body.md` to write a
standalone `$RUN_DIR/pr-body.md`. It must replace rather than append the host
repository's pull request template. Put the plain-language explanation first,
move Forge metadata into the trailing details block, and append the exact
`selection.marker` as the final content.

Validate the rendered body before modifying the checkout:

```bash
PYTHONPYCACHEPREFIX="$RUN_DIR/pycache" python3 "$SKILL_DIR/scripts/validate-publication.py" body \
  --selection "$RUN_DIR/proposal-selection.json" \
  --body "$RUN_DIR/pr-body.md" \
  --target-sha "$TARGET_SHA" \
  --json
```

Then review the final body against `prompts/review-pr-body.md`. Any validation
or review finding blocks publication. Do not add with/without-skill evaluation
claims in this workflow.

Set `$SELECTED_PROPOSAL_DIR` to the selected run-local skill directory and
`$SKILL_PATH` to its repository-relative destination before the checkout
validation steps.

For every `create` action, pass `draft: false` explicitly so newly created
Forge PRs open ready for review. Never omit the field or rely on the platform
default.
For a create action, stage only the selected proposal. Before copying, reject
symlinks in its source tree, every existing destination path component, and
every existing destination entry mapped from a selected source entry. Compare
the pending checkout paths with the exact selected source-file manifest and
block on any additional path.

Require a clean checkout through the packaged validator before copying:

```bash
PYTHONPYCACHEPREFIX="$RUN_DIR/pycache" python3 "$SKILL_DIR/scripts/validate-publication.py" clean \
  --repository "$REPOSITORY_DIR" \
  --json
```

After copying the selected proposal, validate that every changed path and byte
matches that source tree and that no selected text contains secret-shaped,
home-path, or raw session metadata:

```bash
PYTHONPYCACHEPREFIX="$RUN_DIR/pycache" python3 "$SKILL_DIR/scripts/validate-publication.py" checkout \
  --repository "$REPOSITORY_DIR" \
  --source "$SELECTED_PROPOSAL_DIR" \
  --destination "$SKILL_PATH" \
  --json
```

After publication, apply `skills-forge` when the label exists and the available
GitHub tools support it. Label lookup, creation, or application failure is
non-blocking because the persistent marker is the authoritative Forge identity.

Read the validated `$RUN_DIR/pr-body.md` verbatim and pass its exact contents as
the complete PR body. Do not summarize, rewrite, preserve, or append the host
template. The trailing
Forge details include the marker, proposal identity, candidate IDs, confidence,
fixed evidence window, target SHA, validation, review findings, and the unknown
trusted-user-diversity limitation. For a partial run, they also disclose
discovered/completed counts, discovery completeness, known or unknown coverage,
omission count and kinds, and fallback status. Never describe partial evidence
as complete.

Do not write a state issue or any other cross-run artifact after publication.
Discard run evidence when the automation finishes.

## Termination

Success requires complete or disclosed-partial extraction, sanitized
current-window evidence, complete open-and-closed PR reconciliation, no more
than one PR create/update, and a persistent valid marker in every published
Forge PR. The final report must derive its extraction status from a successful
`extraction-worker.py status --assert-terminal` result. It must never report
`BLOCKED` when that command fails because status is `running`.

An opt-in coding agent completion guard may run `completion-predicate.py` when
the agent tries to stop. It enforces the same invariant rather than replacing
it: the guard is satisfied only by terminal `complete` or `partial` controller
status, and reports `incomplete` with the pending action IDs or the exact
blocker for every other state.

Fail explicitly on undisclosed omissions, malformed PR metadata, leakage,
unsafe proposals, missing required publication tools, or blocked publication.
Label tools are optional and must never interrupt extraction, publication, or a
no-proposal result. Never fabricate evidence. Large repositories still use the
full fixed window by default; long asynchronous runtime alone is not a reason
to sample. Introduce deterministic sampling only after observing a concrete
tool-call, output, context, or automation limit.

## Assets

- `scripts/extraction-worker.py`: deterministic extraction driver, bounded
  worker protocol, and terminal assertion.
- `scripts/extraction-controller.py`: resumable run-local extraction state,
  fast primary queries, explicit partial omissions, and the machine-readable
  `diagnostics` snapshot.
- `scripts/run-marker.py`: run-local completion marker lifecycle and the stable
  completion-guard launcher.
- `scripts/completion-predicate.py`: deterministic, side-effect-free coding
  agent completion verdict derived from controller state.
- `scripts/materialize-session-query.py`: exact current-session result
  materialization into controller JSON artifacts.
- `scripts/checkpoint-completed-batches.py`: idempotent progressive
  normalization, sanitization, ledger promotion, and raw-artifact cleanup.
- `scripts/merge-sanitized-primitives.py`: sanitized batch-ledger merge and
  terminal extraction coverage attachment.
- `scripts/aggregate-primitives.py`: current-run deduplication and scoring.
- `scripts/proposal-ledger.py`: PR marker parsing, cataloging, reconciliation,
  and one-mutation selection.
- `scripts/validate-publication.py`: final PR-body, clean-checkout,
  selected-path, content-equality, and leakage validation.
- `scripts/session_queries.py`: materialized session queries and opt-in
  tool-event fallback SQL.
- `reference/extraction-worker.md`: worker protocol, runtime boundary, measured
  cycle reduction, and the bounded per-action fallback.
- `prompts/author-pr-body.md`: plain-language PR body contract.
- `prompts/review-pr-body.md`: final PR body evidence and safety review.
- `reference/cca-completion-guard.md`: coding agent completion guard command,
  environment, verdict, and configuration contract.
- `assets/schemas.json`: extraction, worker protocol, candidate, and PR
  metadata contracts.

**Abstraction level:** strategic
