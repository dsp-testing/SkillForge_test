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
- `windowHours`: default `168` (seven days);
- `discoveryPageSize`: default `100`;
- `sessionBatchSize`: default `25`;
- `toolPageSize`: default `500`;
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

Initialize `extraction-controller.py` with the interface defaults. Its primary
strategy is:

1. select sessions updated inside the fixed window, returning only `id` and
   `updated_at`, ordered by `(updated_at, id)` for keyset pagination, with an
   exact SQL repository predicate in addition to mandatory tool-level scope;
2. fetch exact-ID `sessions` metadata in batches of 100;
3. fetch bounded `session_refs` and `session_files`;
4. fetch relevant `tool_requests` directly with pages of 500. Preserve
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

Every primary discovery query requests `discoveryPageSize + 1` rows. Accept the
first page-size rows and continue after the last `(updated_at, id)` pair.
Process every successful page before requesting the next page.

Retry only non-timeout transient network, rate-limit, or server failures, at
most once. Never repeat an identical timed-out query. Primary discovery
timeout before the first successful page uses adaptive time splitting down to
`minWindowMinutes`. A timeout after successful pages discloses the uncollected
remainder without discarding accepted sessions.

Every query result must be recorded through `extraction-controller.py`. Never
retry manually, alter controller SQL, or continue after a blocked state. Pass
the action's `description` unchanged to `session_store_sql`; it equals the
stable `actionId`.

When query rows are returned to the agent rather than written to `outputPath`,
use the packaged materializer. It matches the controller SQL in the current
automation session's local `events.jsonl`, follows runtime spill receipts, and
atomically writes JSON. This local log is transport for the current result,
not a repository-wide SQL query against `events`.

```bash
RESULT_PATH="$(
  python3 "$SKILL_DIR/scripts/materialize-session-query.py" \
    --actions "$RUN_DIR/actions.json" \
    --action "$ACTION_ID"
)"

python3 "$SKILL_DIR/scripts/extraction-controller.py" record-success \
  --state "$RUN_DIR/extraction-state.json" \
  --action "$ACTION_ID" \
  --result "$RESULT_PATH" \
  --out "$RUN_DIR/extraction-state.next.json"
mv "$RUN_DIR/extraction-state.next.json" "$RUN_DIR/extraction-state.json"
```

Do not manually transcribe returned rows. If materialization reports
`query handoff mismatch`, record a handoff failure so the controller replaces
the exact-ID batch with smaller queries:

```bash
python3 "$SKILL_DIR/scripts/extraction-controller.py" record-failure \
  --state "$RUN_DIR/extraction-state.json" \
  --action "$ACTION_ID" \
  --reason "session_store_sql query handoff mismatch" \
  --error-kind handoff \
  --out "$RUN_DIR/extraction-state.next.json"
mv "$RUN_DIR/extraction-state.next.json" "$RUN_DIR/extraction-state.json"
```

For any other materialization failure, retry the materializer once after
confirming the tool call completed. Then record the integration blocker:

```bash
python3 "$SKILL_DIR/scripts/extraction-controller.py" record-artifact-failure \
  --state "$RUN_DIR/extraction-state.json" \
  --action "$ACTION_ID" \
  --reason "session query rows could not be materialized as JSON" \
  --out "$RUN_DIR/extraction-state.next.json"
mv "$RUN_DIR/extraction-state.next.json" "$RUN_DIR/extraction-state.json"
```

Artifact failures never split, omit, or activate fallback. Extract completed
discovery partitions before requesting unresolved partitions so slow windows
do not starve analysis of evidence already found.

Generate bounded action batches with:

```bash
python3 "$SKILL_DIR/scripts/extraction-controller.py" next \
  --state "$RUN_DIR/extraction-state.json" \
  --parallel \
  --out "$RUN_DIR/actions.json"
```

Discovery manifests contain one action. Post-discovery manifests contain up to
`maxConcurrentBatches` actions from distinct session batches. Execute those
queries concurrently and record results sequentially by exact action ID, never
by completion order. Record completed successes before terminal failures. The
controller is the only writer of run-local extraction state.

Immediately before any final response, publication decision, or run-directory
cleanup, require:

```bash
python3 "$SKILL_DIR/scripts/extraction-controller.py" assert-terminal \
  --state "$RUN_DIR/extraction-state.json"
```

If this command fails because status is `running`, a final response and cleanup
are forbidden. Read the pending action IDs from its error, invoke `next`, execute
the actions, and record every outcome. Repeat until `assert-terminal` succeeds.

A run is `BLOCKED` only when `assert-terminal` succeeds and reports controller
status `blocked`. Never translate `running`, pending work, elapsed time, action
volume, or an unrelated helper-shell/display error into `BLOCKED`. Fix or omit
nonessential display commands and continue from the persisted controller state.
Do not delete `$RUN_DIR` while status is `running`.

Once the controller is initialized, do not stop for label, target-identity, or
publication-tool checks while its status remains `running`.

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

### 4. Normalize, sanitize, and aggregate this run

Normalize completed batches, derive primitives, sanitize them, and checkpoint
the run ledger idempotently by `evidenceKey`. Pass extraction state to
`merge-sanitized-primitives.py` so complete or partial coverage follows the
evidence into candidate generation.

Raw commands and source content remain ephemeral. `aggregate-primitives.py`
deduplicates only the current run and emits patterns and candidates; it does
not read or write durable Forge state:

```bash
python3 "$SKILL_DIR/scripts/aggregate-primitives.py" \
  --in "$RUN_DIR/sanitized-primitives.json" \
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
skills and the complete Forge PR catalog. Every promoted proposal body must
contain the exact marker produced by:

```bash
python3 "$SKILL_DIR/scripts/proposal-ledger.py" marker \
  --proposal "$PROPOSAL_JSON" \
  --out "$RUN_DIR/proposal-marker.md"
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
After publication, apply `skills-forge` when the label exists and the available
GitHub tools support it. Label lookup, creation, or application failure is
non-blocking because the persistent marker is the authoritative Forge identity.

The PR body includes the marker, proposal identity, candidate IDs, confidence,
fixed evidence window, target SHA, validation, review findings, and the
unknown trusted-user-diversity limitation. For a partial run, also disclose
discovered/completed counts, discovery completeness, known or unknown coverage,
omission count and kinds, and fallback status. Never describe partial evidence
as complete.

Do not write a state issue or any other cross-run artifact after publication.
Discard run evidence when the automation finishes.

## Termination

Success requires complete or disclosed-partial extraction, sanitized
current-window evidence, complete open-and-closed PR reconciliation, no more
than one PR create/update, and a persistent valid marker in every published
Forge PR. The final report must derive its extraction status from the successful
`assert-terminal` result. It must never report `BLOCKED` when that command
fails because status is `running`.

Fail explicitly on undisclosed omissions, malformed PR metadata, leakage,
unsafe proposals, missing required publication tools, or blocked publication.
Label tools are optional and must never interrupt extraction, publication, or a
no-proposal result. Never fabricate evidence. Large repositories still use the
full fixed window by default; long asynchronous runtime alone is not a reason
to sample. Introduce deterministic sampling only after observing a concrete
tool-call, output, context, or automation limit.

## Assets

- `scripts/extraction-controller.py`: resumable run-local extraction state,
  fast primary queries, and explicit partial omissions.
- `scripts/materialize-session-query.py`: exact current-session result
  materialization into controller JSON artifacts.
- `scripts/aggregate-primitives.py`: current-run deduplication and scoring.
- `scripts/proposal-ledger.py`: PR marker parsing, cataloging, reconciliation,
  and one-mutation selection.
- `scripts/session_queries.py`: materialized session queries and opt-in
  tool-event fallback SQL.
- `assets/schemas.json`: extraction, candidate, and PR metadata contracts.

**Abstraction level:** strategic
