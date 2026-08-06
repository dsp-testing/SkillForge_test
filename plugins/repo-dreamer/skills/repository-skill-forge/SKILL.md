---
name: repository-skill-forge
description: "Incrementally mine repo-scoped CLI, CCA, and CCR sessions for recurring workflows, safely consolidate evidence across scheduled runs, and create at most one reviewable repository-skill PR per run. Use for Dreaming-based repository Forge, multi-session skill discovery, or repo-level skill proposals."
user-invocable: true
---

# Repository Skill Forge

Mine bounded repository-scoped session evidence, derive and persist safe workflow
patterns, evaluate them against repository behavior, and publish at most one
reviewable skill proposal through approved GitHub MCP tools.

## Purpose

Provide the repository-level, scheduled successor to the user-and-branch-scoped
`skill-forge` workflow without weakening repository isolation, privacy, evidence
quality, or publication controls.

## Conditions (C)

Use this skill only when:

- `session_store_sql` supports repository-scoped queries for the target repo;
- the host supplies a repository-isolated durable state location;
- Python 3.10 or later is available for packaged deterministic scripts;
- GitHub repository reads use GitHub MCP rather than `gh` or authenticated
  scripts;
- the run has an absolute scratch directory outside the target checkout.

The baseline works in one agent context. Subagents are optional and must not be
required for extraction, confidence calculation, or validation.

Do not claim trusted multi-user diversity. Repository session data currently
lacks a reliable actor identity, so user diversity remains explicitly unknown.

## Interface (R)

Inputs:

- `repository`: target `owner/name`;
- `runId`: the Dream execution identifier;
- `runDir`: isolated scratch directory for this execution;
- `statePath`: repository-isolated durable state JSON;
- `windowEnd`: exclusive UTC end of this run;
- `overlapHours`: default `24`;
- `initialBackfillDays`: default `30`;
- `discoveryPageSize`: default `500`;
- `sessionBatchSize`: default `100`;
- `toolPageSize`: default `500`;
- `maxRows`: default `1000`;
- `maxArtifactBytes`: default `10000000`;
- `minWindowMinutes`: default `15`;
- `activeDays`: default `90`;
- `staleDays`: default `180`;
- `publicationPrBudget`: default `1`;
- `publicationCooldown`: default equal to the Dream schedule cadence.

Outputs:

- deterministic run artifacts under `$RUN_DIR`;
- next-state JSON written to a temporary path for durable promotion;
- zero or more qualified candidates retained in state;
- at most one new repository-skill PR per run;
- an explicit no-op or failure summary when nothing can be published.

## Policy (π)

### 1. Resolve isolated run and state scope

Set `SKILL_DIR` to this skill folder. Create `$RUN_DIR` outside the target
checkout. Record repository, run ID, evidence window, target default-branch SHA,
thresholds, and script version in `$RUN_DIR/scope.json`.

Read the prior state only from the repository-isolated `statePath`. Reject state
whose repository does not exactly match the target. If no state exists, set the
window start to `windowEnd - initialBackfillDays`. Otherwise set it to
`cursor - overlapHours`.

Never advance or replace durable state until every deterministic stage,
proposal review, and publication-ledger update for the run has completed.

### 2. Resolve the default branch and initialize bounded extraction

Use GitHub MCP to resolve the default branch and its current SHA before
sanitization. Record both in `scope.json`; the default branch name is used only
to categorize evidence and is not copied into persistent branch metadata.

Initialize the run-local extraction state:

```bash
python3 "$SKILL_DIR/scripts/extraction-controller.py" init \
  --out "$RUN_DIR/extraction-state.json" \
  --repository "$REPOSITORY" \
  --start "$WINDOW_START" \
  --end "$WINDOW_END" \
  --run-dir "$RUN_DIR" \
  --discovery-page-size "$DISCOVERY_PAGE_SIZE" \
  --session-batch-size "$SESSION_BATCH_SIZE" \
  --tool-page-size "$TOOL_PAGE_SIZE" \
  --max-rows "$MAX_ROWS" \
  --max-artifact-bytes "$MAX_ARTIFACT_BYTES" \
  --min-window-minutes "$MIN_WINDOW_MINUTES"
```

The extraction state is run-local and must not be promoted as durable Forge
state. It records adaptive time partitions, keyset cursors, session batches,
tool-page cursors, cumulative query, row, relevant-tool-call, and artifact-byte
counters, retries, and terminal blockers.

### 3. Execute the adaptive extraction loop

Ask the controller for one deterministic action:

```bash
python3 "$SKILL_DIR/scripts/extraction-controller.py" next \
  --state "$RUN_DIR/extraction-state.json" \
  --out "$RUN_DIR/next-action.json"
```

When the action kind is `discovery`, `metadata`, `refs`, `files`, or
`tool-calls`, execute its `sql` with `session_store_sql`, always passing
`repo: "$REPOSITORY"`. Save the returned rows array exactly at the action's
`outputPath`, then record success into a temporary state and atomically promote
it:

```bash
python3 "$SKILL_DIR/scripts/extraction-controller.py" record-success \
  --state "$RUN_DIR/extraction-state.json" \
  --action "$RUN_DIR/next-action.json" \
  --result "$ACTION_OUTPUT_PATH" \
  --out "$RUN_DIR/extraction-state.next.json"
```

On a query timeout or upstream failure, use `record-failure` with a concise
reason instead. Retry action identities include page size and retry generation,
so reducing a page cannot collide with the failed action it replaces. The
controller recursively splits discovery time windows, splits session batches,
then reduces single-session tool-page sizes. If a single-session,
single-tool-page unit still fails, the run is blocked. Never skip the unit or
advance beyond it.

Every paged query requests `limit + 1`; the extra row proves more pages exist
and is not consumed in the current page. Session metadata, refs, touched files,
and tool calls are separate bounded queries so no correlated child-table
aggregation can dominate a small session batch. Tool queries include only
Forge-relevant calls (`bash`, `shell`, `powershell`, `create`, `edit`, and
`apply_patch`) and latest completion records for those calls. Do not fetch
turns, assistant messages, unrelated tool calls, or full event streams.

Every query must also carry a source-time floor and ceiling. Discovery uses the
overlap condition on `updated_at`; after metadata is loaded, child queries use
the earliest actual session `created_at` in the batch as their floor and the
latest actual session `updated_at` plus one minute, capped by the partition end,
as their ceiling. Apply those bounds directly to
`session_refs.created_at`, `session_files.first_seen_at`, and
`events.timestamp`. Because `tool_requests` has no timestamp, join it through
the exact batch IDs in a `sessions` target that is bounded by `created_at`.
`LIMIT` is only an output bound and never substitutes for time-based source
pruning.

Temporary-file transport may be used when a successful tool response is too
large for the conversation. It does not fix database-execution timeouts; those
must go through adaptive subdivision.

Repeat until the controller returns `{"kind":"done","status":"complete"}`. A
blocked artifact may be produced only when the controller returns
`{"kind":"done","status":"blocked","blocker":{...}}` and the extraction state
has both `status: "blocked"` and a non-empty terminal blocker.

### 4. Normalize, derive, sanitize, and checkpoint each batch

For each completed extraction batch, normalize its metadata artifact and all
accepted tool-page artifacts:

```bash
TOOL_ARGS=()
for path in "${TOOL_PAGE_PATHS[@]}"; do
  TOOL_ARGS+=(--tool-in "$path")
done
REF_ARGS=()
for path in "${REF_PAGE_PATHS[@]}"; do
  REF_ARGS+=(--refs-in "$path")
done
FILE_ARGS=()
for path in "${FILE_PAGE_PATHS[@]}"; do
  FILE_ARGS+=(--files-in "$path")
done

python3 "$SKILL_DIR/scripts/normalize-sessions.py" \
  --metadata-in "$METADATA_PATH" \
  "${REF_ARGS[@]}" \
  "${FILE_ARGS[@]}" \
  "${TOOL_ARGS[@]}" \
  --out "$BATCH_DIR/normalized-sessions.json" \
  --repository "$REPOSITORY" \
  --window-start "$WINDOW_START" \
  --window-end "$WINDOW_END" \
  --limit-sessions "$SESSION_BATCH_SIZE"

python3 "$SKILL_DIR/scripts/derive-primitives.py" \
  --in "$BATCH_DIR/normalized-sessions.json" \
  --out "$BATCH_DIR/primitives.raw.json"

python3 "$SKILL_DIR/scripts/sanitize-evidence.py" \
  --in "$BATCH_DIR/primitives.raw.json" \
  --out "$BATCH_DIR/primitives.sanitized.json" \
  --report "$BATCH_DIR/leakage-report.json" \
  --main-branch "$DEFAULT_BRANCH"

LEDGER_ARGS=()
if [[ -f "$RUN_DIR/primitives.sanitized.json" ]]; then
  LEDGER_ARGS=(--ledger-in "$RUN_DIR/primitives.sanitized.json")
fi
python3 "$SKILL_DIR/scripts/merge-sanitized-primitives.py" \
  "${LEDGER_ARGS[@]}" \
  --batch "$BATCH_ID" "$BATCH_DIR/primitives.sanitized.json" \
  --out "$RUN_DIR/primitives.sanitized.next.json"
```

Stop on every blocking finding. Raw command and script content may exist only in
ephemeral pre-sanitization artifacts. Persistent evidence and model inputs must
not retain source script bodies. Never author a skill by copying a session
script verbatim.

Atomically promote the sanitized ledger before deleting raw query, normalized,
or unsanitized batch artifacts. The merge is idempotent by batch ID and
`evidenceKey`, so resuming an interrupted run cannot double-count evidence.

Normalization covers CLI, CCA, and CCR but does not assume identical success
signals. Shell outcomes are scored only when an explicit process exit code is
present. Tool completion alone does not prove process success. Tool calls are
ordered by completion timestamp so script-producing edits are available before
later shell execution.

Each primitive is keyed by session, tool call, fingerprint, and timestamp so an
overlap-window replay does not double-count it. Repeated executions within one
session remain one distinct-session contribution.

### 5. Resolve mainline corroboration through GitHub MCP

Use GitHub MCP to:

- inspect default-branch `.github/skills/**`, repository instructions, and
  relevant documentation;
- determine which PR references in the sanitized evidence are merged.

Save the merged PR numbers as a JSON array in `$RUN_DIR/merged-prs.json`. Do not
use `gh`, authenticated `curl`, or credentials from a packaged script.

### 6. Merge incremental state and rank candidates

```bash
STATE_ARGS=()
if [[ -f "$STATE_PATH" ]]; then
  STATE_ARGS=(--state-in "$STATE_PATH")
fi

python3 "$SKILL_DIR/scripts/aggregate-primitives.py" \
  --in "$RUN_DIR/primitives.sanitized.json" \
  --out "$RUN_DIR/candidates.json" \
  --state-out "$RUN_DIR/state.next.json" \
  "${STATE_ARGS[@]}" \
  --repository "$REPOSITORY" \
  --as-of "$WINDOW_END" \
  --next-cursor "$WINDOW_END" \
  --merged-prs "$RUN_DIR/merged-prs.json" \
  --active-days "$ACTIVE_DAYS" \
  --stale-days "$STALE_DAYS"

python3 "$SKILL_DIR/scripts/validate-candidates.py" \
  "$RUN_DIR/candidates.json"
```

Initial eligibility requires at least three distinct sessions, two distinct
days, three known outcomes, 0.7 success, 0.5 scored coverage, and either two
merged PRs or two mainline-corroborated observations. Record every numerator,
denominator, unknown outcome, and hold reason.

### 7. Cluster by repository subject

The deterministic `subjectKey` is a first-pass grouping hint. When several
candidates are eligible, use `prompts/cluster-candidates.md` to group them by
repository subject area. Branch is evidence metadata, never the grouping axis.

This may run in the orchestrator context. If Dreaming later guarantees isolated
subagents, subject buckets may be reviewed in parallel, but the output must
still be an exact partition of candidate IDs.

Save the result to `$RUN_DIR/clusters.json`, then validate it:

```bash
python3 "$SKILL_DIR/scripts/validate-clusters.py" \
  --candidates "$RUN_DIR/candidates.json" \
  --clusters "$RUN_DIR/clusters.json"
```

### 8. Author and independently review proposals

For each eligible subject in ranked order:

1. Compare it with default-branch skills and instructions.
2. Apply `prompts/author-proposal.md`.
3. Make one create, improve, merge, or hold decision.
4. Write promoted files only under
   `$RUN_DIR/proposals/<candidate-id>/<skill-name>/`.
5. Validate each generated skill with `scripts/validate-skill.py`.
6. Apply `prompts/review-proposal.md` in a clean review context when available,
   or as a distinct review phase in the orchestrator.
7. Save the review and proposal manifest.
8. Validate the manifest with `scripts/validate-proposal.py`.

Do not modify the checkout during evidence processing or authoring.

### 9. Select at most one publication

Rank valid proposals by distinct sessions, success rate, mainline
corroboration, and recency. Publish no more than one new PR per repository per
run. Persist other qualified candidates for later reconsideration.

Check proposal history before publishing:

- update an existing open PR for the same materially changed candidate;
- do not duplicate an unchanged candidate;
- do not automatically re-propose a rejected candidate without materially new
  evidence or a policy change;
- require the configurable cooldown, defaulting to the Dream schedule cadence.

Check duplicate and cooldown state deterministically:

```bash
python3 "$SKILL_DIR/scripts/proposal-ledger.py" check \
  --state "$RUN_DIR/state.next.json" \
  --candidate-id "$CANDIDATE_ID" \
  --candidate-version "$CANDIDATE_VERSION" \
  --now "$WINDOW_END" \
  --cooldown-hours "$PUBLICATION_COOLDOWN_HOURS" \
  --out "$RUN_DIR/publication-check.json"
```

A nonzero result means the proposal remains queued and no new PR is opened.
When the result has `action: "update"`, update `existingPrRef` even during the
new-PR cooldown. The cooldown applies only when `action` is `"create"`.

### 10. Publish through approved GitHub MCP tools

Discover the GitHub MCP tools available to create a branch, write the proposed
files, and create or update a PR. The PR body must include confidence metrics,
evidence window, target SHA, leakage and conflict results, validation status,
and the limitation that trusted user diversity is unknown.

If required PR-writing tools are unavailable, retain the publishable proposal
bundle, mark publication blocked, and do not fall back to `gh` or independently
authenticated scripts.

### 11. Commit durable state only after success

Record proposal history after publication or an explicit held/blocked outcome:

```bash
python3 "$SKILL_DIR/scripts/proposal-ledger.py" record \
  --state "$RUN_DIR/state.next.json" \
  --candidate-id "$CANDIDATE_ID" \
  --candidate-version "$CANDIDATE_VERSION" \
  --decision "$DECISION" \
  --status "$PUBLICATION_STATUS" \
  --published-at "$WINDOW_END" \
  --pr-ref "$PR_REF" \
  --out "$RUN_DIR/state.final.json"
```

Atomically replace the repository-isolated durable state with
`state.final.json` only after the run has completed its intended publication or
explicit no-op outcome. Recording a held or blocked evaluation must not erase an
existing open or published PR reference.

Write `$RUN_DIR/forge-summary.md` with evidence counts, coverage, candidate
decisions, publication budget/cooldown, state transition, and all blockers.

## Termination (T)

Success requires:

- evidence was queried with repository scope and was not truncated;
- normalized evidence and persistent state contain no blocking leakage;
- overlap replay produced no duplicate evidence keys;
- candidate metrics expose scored and unscored denominators;
- default-branch comparison used GitHub MCP;
- at most one new PR was opened;
- durable state advanced only after successful completion;
- every proposal and no-op is recorded in the run summary.

Failure is explicit when state scope mismatches, evidence is incomplete,
normalization fails, leakage is detected, candidate validation fails, a proposal
is unsafe, or required publication tools are unavailable. Never fabricate
evidence, silently advance the cursor, or report a PR that was not created.

## Always do

- Keep every run and durable state repository-isolated.
- Re-query an overlap window and deduplicate with stable evidence keys.
- Preserve a rebuildable sanitized evidence ledger.
- Treat user diversity as unknown until a trusted actor identifier exists.
- Prefer improving or merging existing skills over duplicates.
- Keep PR publication idempotent and within the configured budget.

## Never do

- Never use `gh`, authenticated `curl`, or scripts that obtain GitHub credentials.
- Never persist raw session script bodies in candidate or state artifacts.
- Never infer process success from tool completion alone.
- Never let repeated calls in one session satisfy distinct-session confidence.
- Never publish branch-only behavior as a repository skill.
- Never open more than one new Forge PR for a repository in one run.

## Gotchas / edge cases

- Repo-scoped stores may expose different outcome detail for CLI, CCA, and CCR.
- Late events require an overlap window, but stable evidence keys prevent double counting.
- A busy repository may require narrower time partitions, smaller session batches,
  or smaller tool pages rather than a silently larger query.
- Merged PR status must be resolved through GitHub MCP after session normalization.
- Old evidence remains rebuildable but stops contributing after the stale window.
- A qualified candidate can remain queued when another proposal consumes the PR budget.
- Publication may be blocked even when a proposal is valid if MCP write tools are absent.

## Assets and scripts

- `scripts/build-session-query.py`: generate bounded repo-scoped evidence SQL.
- `scripts/session_queries.py`: shared discovery, metadata, and relevant-tool SQL builders.
- `scripts/extraction-controller.py`: plan, subdivide, and checkpoint bounded extraction.
- `scripts/normalize-sessions.py`: normalize CLI, CCA, and CCR query rows.
- `scripts/derive-primitives.py`: derive stable command and script fingerprints.
- `scripts/sanitize-evidence.py`: redact personal paths and fail on secret-shaped data.
- `scripts/merge-sanitized-primitives.py`: idempotently checkpoint sanitized batch evidence.
- `scripts/aggregate-primitives.py`: merge state, deduplicate overlap, and score candidates.
- `scripts/validate-candidates.py`: validate confidence and raw-evidence invariants.
- `scripts/validate-clusters.py`: enforce exact candidate partitioning by subject.
- `scripts/validate-proposal.py`: enforce review, cooldown, deduplication, and PR budget.
- `scripts/proposal-ledger.py`: check cooldown/duplicates and record PR history.
- `scripts/validate-skill.py`: validate generated skill structure.
- `assets/schemas.json`: machine-readable artifact contracts.
- `prompts/*.md`: semantic clustering, authoring, and review policy.

## Scope boundaries

This skill owns repository-scoped evidence consolidation, candidate selection,
proposal authoring, validation, and GitHub-MCP publication instructions. The
Dreaming host owns scheduling, repository-isolated durable storage, tool
availability, authentication, and execution-level artifact retention.

**Abstraction level:** strategic
