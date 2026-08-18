---
name: repository-skill-forge
description: Incrementally mine repository sessions, persist compact issue-backed evidence, and publish at most one queued skill proposal per run.
user-invocable: true
---

# Repository Skill Forge

Mine bounded repository-scoped session evidence with the deployed fast query
strategy, retain compact sanitized state in one repository issue, and publish
at most one reconciled skill proposal per run.

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
- `initialBackfillDays`: default `7`;
- `overlapHours`: default `24`;
- `discoveryPageSize`: default `100`;
- `sessionBatchSize`: default `100`;
- `toolPageSize`: default `500`;
- `maxRows`: default `1000`;
- `maxArtifactBytes`: default `10000000`;
- `maxQueryRetries`: default `1` retry for non-timeout transient failures;
- `minWindowMinutes`: default `15`;
- `maxConcurrentBatches`: default `3`;
- `enableToolEventFallback`: default `false`;
- `allowPartial`: default `true`;
- `activeDays`: default `90`;
- `staleDays`: default `180`;
- `stateIssueTitle`: exact default `Repository Skill Forge state`;
- `issueStateMaxBytes`: default `60000`.

There is no host `statePath`. Durable state lives in one dedicated issue for
the repository. The state issue and every proposal PR use `skills-forge`.

## Policy

### 1. Resolve the durable state issue

Use GitHub MCP to find the repository's single open or closed issue whose title
exactly equals `Repository Skill Forge state` and which has the `skills-forge`
label. Create the label when supported; otherwise require it to be
pre-provisioned. Never continue with an unlabeled issue or PR.

Create the issue only when none exists. Fail if multiple matching issues exist.
Preserve all human-maintained issue text.

Managed state uses visible plain-text markers that approved GitHub MCP reads
preserve:

```text
repository-skill-forge-state:v2:begin
{
    "schemaVersion": 2,
    "stateVersion": 1,
    "scope": { "kind": "repository", "repository": "owner/name" },
    "cursor": null,
    "updatedAt": "<windowEnd>",
    "observations": [],
    "fingerprintCatalog": {},
    "proposalQueue": [],
    "proposalHistory": {}
}
repository-skill-forge-state:v2:end
```

MCP may repeatedly HTML-encode the JSON payload. The parser first accepts raw
JSON, then applies at most three HTML-decoding passes while JSON remains
invalid. It also accepts the fenced and HTML-comment v1 formats for migration;
every render writes the plain v2 format.

Parse an existing body with:

```bash
python3 "$SKILL_DIR/scripts/issue-state.py" parse \
  --body-in "$RUN_DIR/state-issue-body.md" \
  --state-out "$RUN_DIR/state.json" \
  --repository "$REPOSITORY" \
  --max-bytes "$ISSUE_STATE_MAX_BYTES"
```

Reject repository/version mismatches, malformed or duplicate managed blocks,
unsupported nested shapes, raw session fields, commands, local paths, secrets,
and oversized state. Immediately read back and parse a newly created or updated
issue before continuing. If state readback or parsing fails, stop without
modifying the issue body again; do not replace it with a blocked-run note.

### 2. Select the incremental window

When the cursor is null, set `windowStart = windowEnd - 7 days`. Otherwise use
`windowStart = cursor - 24 hours`. Deduplicate the overlap by stable
`evidenceKey`. Resolve the default branch and SHA through GitHub MCP.

Do not advance the issue cursor until extraction, validation, reconciliation,
the selected publication outcome, and the issue update all succeed.

### 3. Run the deployed fast extraction strategy

Initialize `extraction-controller.py` with the interface defaults. Its primary
strategy is:

1. select sessions updated inside the time window, returning only `id` and
   `updated_at`, ordered by `(updated_at, id)` for keyset pagination, with an
   exact SQL repository predicate in addition to the mandatory tool-level
   repository scope;
2. fetch exact-ID `sessions` metadata in batches of 100;
3. fetch bounded `session_refs` and `session_files`;
4. fetch relevant `tool_requests` with pages of 500 and time-bounded completion
   events.

Tool requests are selected by exact session ID. A session created before the
incremental window but updated inside it must not be excluded.
The materialized `sessions.updated_at` value is the timestamp of the session's
latest event and is the workflow's completion-time proxy. Sessions are
resumable, so the workflow does not require a separate `completed_at` value.

Do not fetch turns, assistant messages, or unrelated tools. Do not run a
separate repository-wide count query; discovery is the authoritative inventory,
and zero discovered sessions is a successful no-evidence outcome. Every primary
discovery query requests `discoveryPageSize + 1` rows. With the default, when
101 rows return, accept the first 100 and continue after the last
`(updated_at, id)` pair. Process each successful page before requesting the
next page. The 24-hour overlap covers sessions that move between extraction
windows, and stable session IDs deduplicate overlap.

Retry only non-timeout transient network, rate-limit, or server failures, at
most once. No timeout repeats the identical action. Primary discovery timeout
before the first successful page uses adaptive time splitting down to
`minWindowMinutes`. A timeout after one or more pages discloses the uncollected
remainder without discarding accepted sessions. There is no `session.shutdown`
discovery fallback.
Exact-session metadata comes only from the materialized `sessions` table.
Syntax, schema, validation, authorization, and genuinely unknown failures are
not retryable. Discovery has no global failure-count or elapsed-time budget.
With `--fail-on-omission`, any irreducible discovery failure blocks.

Every query success or failure must be recorded through
`extraction-controller.py`. Never retry a query manually, alter controller SQL
ad hoc, or continue after the controller returns a blocked state.
Extract completed discovery partitions before requesting the next unresolved
partition. This prevents slow or omitted windows from starving metadata,
evidence analysis, candidate generation, and proposal generation for sessions
already found. Continue invoking the controller while its state is `running`;
the agent must not independently declare the run blocked because remaining work
is slow or numerous.

Generate the next bounded action batch with:

```bash
python3 "$SKILL_DIR/scripts/extraction-controller.py" next \
  --state "$RUN_DIR/extraction-state.json" \
  --parallel \
  --out "$RUN_DIR/actions.json"
```

Discovery manifests contain exactly one action because pages and timeout
partitions are cursor-dependent. Post-discovery manifests contain up to
`maxConcurrentBatches` actions from different session batches. Execute those
queries concurrently, but record each success or failure sequentially through
the controller using its action ID. Issued actions remain persisted until their
individual result is recorded, so recording one action cannot invalidate its
siblings. Record completed successes before failures, and terminal failures
last, so a blocked action cannot prevent sibling results from being persisted.
Never execute two actions for the same batch concurrently. The controller is
the only writer of extraction state.

Without `--parallel`, `next` retains the single-action interface. If `--out` is
omitted, `next` prints the action or manifest to stdout. `--action` accepts a
current parallel action ID when an individual action file is unavailable.

### 4. Handle irreducible query failures

Tool-event fallback is opt-in. With the default
`enableToolEventFallback=false`, a timed-out post-discovery unit is omitted
without an identical retry or page-size reduction when partial mode is enabled.

When `enableToolEventFallback=true`:

1. a timed-out exact-session tool unit immediately switches from
   `tool_requests` to bounded `tool.execution_start` and
   `tool.execution_complete` events; larger timed-out tool batches split only
   the tool stage and reuse completed metadata, reference, and file artifacts;
   a failed single-session event fallback is omitted;
2. timed-out exact-session metadata is omitted because the materialized
   `sessions` table is authoritative;
3. timed-out `session_refs` and `session_files` units are omitted immediately
   because reducing `LIMIT` does not avoid their expensive scan and ordering.

Fallback applies only to the failed tool extraction unit and does not query
outside the selected evidence window.

With default `allowPartial=true`, irreducible timeout or exhausted transient
discovery windows are recorded by time range, and irreducible post-discovery
units are recorded with repository-salted session hashes. Use
`--fail-on-omission` for fail-closed
behavior. A disclosed partial run is successful and advances durable state after
validation, reconciliation, and the publication or no-publication decision.

### 5. Normalize, sanitize, and preserve coverage

Normalize completed batches, derive primitives, sanitize them, and checkpoint
the run ledger idempotently by `evidenceKey`. Pass the extraction state to
`merge-sanitized-primitives.py` so complete or partial coverage follows the
evidence into candidate generation.

Raw commands and source content remain ephemeral. Persistent observations keep
only evidence/fingerprint hashes, repository-salted session and branch hashes,
timestamps, outcome, surface, kind, bounded path families, and repository
references. Durable state also keeps a bounded catalog of versioned, sanitized
structural signatures keyed by fingerprint. This allows later runs to explain
and aggregate historical patterns without retaining raw commands or source
content. Legacy v2 states without `fingerprintCatalog` migrate as an empty
catalog on their next parse and render.

### 6. Build compact state and candidates

Run `aggregate-primitives.py` with the parsed issue state. It deduplicates the
overlap, expires stale observations, preserves the proposal queue/history, and
writes schema-version-2 state.

Eligibility remains at least three distinct sessions, two distinct days, three
known outcomes, 0.7 success, 0.5 scored coverage, and either two merged PRs or
two mainline-corroborated observations. Partial coverage does not change these
thresholds and must remain visible.

### 7. Author, review, and queue proposals

Cluster eligible candidates by repository subject. Assign each proposal a
stable `proposalKey`, include all cluster `candidateIds`, and derive a
`proposalVersion` from evidence plus generated content.

Validate and independently review every proposal. Enqueue validated,
non-overlapping proposals with deterministic ranks. A newer version replaces
the queued version for the same `proposalKey`; unrelated proposals remain
separate.

### 8. Reconcile and publish sequentially

Reconcile queued proposals against open draft, open ready, closed-unmerged, and
merged PRs. Preserve draft state and proposal history. Run
`proposal-ledger.py select` and create or update at most one proposal PR.
Deferred entries remain queued.

Apply `skills-forge`. The PR body includes proposal identity, candidate IDs,
confidence, window, target SHA, validation, leakage/conflict results, and the
unknown trusted-user-diversity limitation.

For a partial run, also disclose:

- discovered and completed session counts;
- whether discovery was complete;
- session coverage ratio when known, otherwise that coverage is unknown;
- omission count;
- omitted unit kinds;
- whether tool-event fallback was enabled.

Never describe partial evidence as complete.

### 9. Update the state issue

After the selected publication outcome is safely recorded, render and update
the existing state issue once:

```bash
python3 "$SKILL_DIR/scripts/issue-state.py" render \
  --state-in "$RUN_DIR/state.next.json" \
  --body-in "$RUN_DIR/state-issue-body.md" \
  --body-out "$RUN_DIR/state-issue-body.next.md" \
  --repository "$REPOSITORY" \
  --max-bytes "$ISSUE_STATE_MAX_BYTES"
```

Do not advance state after a blocked mutation. A per-run audit comment is
optional.

## Termination

Success requires explicit complete-or-disclosed-partial discovery and
extraction coverage, sanitized compact state, stable overlap deduplication, lifecycle
reconciliation, no more than one PR create/update, and one successful state
issue update.

Fail explicitly on undisclosed omissions, state mismatch, leakage, unsafe
proposal, missing label/write tools, blocked publication, or oversized state.
Never fabricate evidence. An omitted discovery window makes total session
coverage unknown and may permanently exclude patterns from that window.

## Assets

- `scripts/extraction-controller.py`: deployed fast extraction plus optional
  tool-event fallback and explicit partial omissions.
- `scripts/issue-state.py`: strict managed issue-state parser and renderer.
- `scripts/aggregate-primitives.py`: compact observation merge and scoring.
- `scripts/proposal-ledger.py`: stable proposal reconciliation and sequential
  queue selection.
- `scripts/session_queries.py`: materialized session queries and opt-in
  tool-event fallback SQL.
- `assets/schemas.json`: extraction, state, queue, and history contracts.

**Abstraction level:** strategic
