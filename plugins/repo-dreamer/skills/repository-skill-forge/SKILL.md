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
- `discoveryPageSize`: default `500`;
- `sessionBatchSize`: default `100`;
- `toolPageSize`: default `500`;
- `maxRows`: default `1000`;
- `maxArtifactBytes`: default `10000000`;
- `maxQueryRetries`: default `2` retries after the first attempt;
- `minWindowMinutes`: default `15`;
- `enableTargetedFallback`: default `false`;
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
strategy remains the deployed `83efbd19bc` approach:

1. discover sessions by `sessions.updated_at`, keyset ordered by
   `(updated_at, id)`, with pages of 500 and an exact SQL repository predicate
   in addition to the mandatory tool-level repository scope;
2. fetch exact-ID `sessions` metadata in batches of 100;
3. fetch bounded `session_refs` and `session_files`;
4. fetch relevant `tool_requests` with pages of 500 and time-bounded completion
   events.

Tool requests are selected by exact session ID. A session created before the
incremental window but updated inside it must not be excluded.

Do not fetch turns, assistant messages, unrelated tools, or a global shutdown
inventory. Every paged query requests `limit + 1`. Retry the identical failed
action according to `maxQueryRetries`, then split only the failing time window,
session batch, or page.

Every query success or failure must be recorded through
`extraction-controller.py`. Never retry a query manually, alter controller SQL
ad hoc, or continue after the controller returns a blocked state.

### 4. Handle irreducible query failures

Targeted shutdown/event fallback is opt-in. With the default
`enableTargetedFallback=false`, an irreducible post-discovery unit is omitted
after retries and adaptive subdivision.

When `enableTargetedFallback=true`:

1. an irreducible tool unit may switch from `tool_requests` to bounded
   `tool.execution_start` and `tool.execution_complete` events;
2. an irreducible metadata unit may query the exact session's latest bounded
   `session.shutdown`, then derive metadata from that shutdown day.

Fallback applies only to the failing unit. Never switch the run to a global
shutdown inventory.

Discovery is always complete or blocked. With default `allowPartial=true`,
irreducible post-discovery units are recorded with repository-salted session
hashes and omitted. Use `--fail-on-omission` for fail-closed behavior.

### 5. Normalize, sanitize, and preserve coverage

Normalize completed batches, derive primitives, sanitize them, and checkpoint
the run ledger idempotently by `evidenceKey`. Pass the extraction state to
`merge-sanitized-primitives.py` so complete or partial coverage follows the
evidence into candidate generation.

Raw commands and source content remain ephemeral. Persistent observations keep
only evidence/fingerprint hashes, repository-salted session and branch hashes,
timestamps, outcome, surface, kind, bounded path families, and repository
references.

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
- session coverage ratio and omission count;
- omitted unit kinds;
- whether targeted fallback was enabled.

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

Success requires complete discovery, explicit complete-or-disclosed-partial
coverage, sanitized compact state, stable overlap deduplication, lifecycle
reconciliation, no more than one PR create/update, and one successful state
issue update.

Fail explicitly on incomplete discovery, state mismatch, leakage, unsafe
proposal, missing label/write tools, blocked publication, or oversized state.
Never fabricate evidence.

## Assets

- `scripts/extraction-controller.py`: deployed fast extraction plus optional
  targeted fallback and explicit partial omissions.
- `scripts/issue-state.py`: strict managed issue-state parser and renderer.
- `scripts/aggregate-primitives.py`: compact observation merge and scoring.
- `scripts/proposal-ledger.py`: stable proposal reconciliation and sequential
  queue selection.
- `scripts/session_queries.py`: fast primary and opt-in targeted fallback SQL.
- `assets/schemas.json`: extraction, state, queue, and history contracts.

**Abstraction level:** strategic
