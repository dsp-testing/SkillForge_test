---
name: skill-forge
description: "Turn recurring successful workflows from the current user's recent Copilot CLI sessions into new, improved, or merged repository skills. Use when asked to run Forge, create skills from session history, or analyze repeated workflows."
user-invocable: true
---

# Skill Forge

Reproduce the current Forge workflow as a self-contained skill. Mine remote
session evidence for the invoking user, current repository, and current branch;
derive candidates deterministically; then create, improve, merge, or hold a
repository skill.

## Purpose

Package Forge independently from its scheduler and delivery system while
preserving its current evidence scope, thresholds, decision policy, generated
skill structure, and `.github/skills` mutation behavior.

## Conditions (C)

Use this skill when:

- the working directory is a Git repository with a resolvable `owner/name` remote;
- the current branch can be resolved;
- the caller wants Forge to analyze their own Copilot CLI sessions;
- `session_store_sql` can query the caller's remote session history.
- Bash and Python 3.10 or later are available. On Windows, run the packaged
  shell wrapper through Git Bash.

Do not use it for repository-wide, multi-user mining. That is a separate future
scope. Do not silently drop the branch filter or increase the evidence scope.

## Interface (R)

Inputs:

- `repository`: current `owner/name`;
- `branch`: current branch;
- `user`: invoking user;
- `limitSessions`: default `100`;
- `usageThreshold`: default `3`;
- `successThreshold`: default `0.7`;
- `runDir`: an absolute scratch directory outside the target checkout.

Outputs:

- `$RUN_DIR/remote-tool-rows.json`;
- `$RUN_DIR/trajectory-events.json`;
- `$RUN_DIR/candidates.json`;
- `$RUN_DIR/forge-summary.md`;
- create, improve, or merge changes under `.github/skills/**`, when promoted;
- no repository skill change for `hold_as_pattern_only`.

## Policy (π)

### 1. Resolve the current scope

From the repository root:

```bash
SKILL_DIR="<absolute path to this skill directory>"
REPOSITORY="$(git remote get-url origin)"
BRANCH="$(git branch --show-current)"
RUN_DIR="${TMPDIR:-/tmp}/skill-forge-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_DIR"
```

Normalize `REPOSITORY` to `owner/name`. Stop if the repository or branch cannot
be resolved. Never substitute the default branch for the current branch.

### 2. Fetch remote evidence

Generate the user-scoped query:

```bash
"$SKILL_DIR/scripts/fetch-session-evidence.sh" query \
  --repository "$REPOSITORY" \
  --branch "$BRANCH" \
  --limit-sessions 100 \
  > "$RUN_DIR/session-query.sql"
```

Run that SQL with `session_store_sql` using the caller's personal scope. Save
the returned `rows` array, without rewriting field values, to:

```text
$RUN_DIR/remote-tool-rows.json
```

The query already restricts evidence to Copilot CLI sessions, the current
repository, the current branch, and the latest 100 matching sessions. Inspect
the tool result before saving it. If the result reports truncation, stop and
record the run as incomplete; never derive candidates from silently truncated
rows.

### 3. Normalize evidence

```bash
"$SKILL_DIR/scripts/fetch-session-evidence.sh" convert \
  --in "$RUN_DIR/remote-tool-rows.json" \
  --out "$RUN_DIR/trajectory-events.json"
```

Normalization must preserve the current Forge trajectory contract:
`command`, `result`, and `cache` events. Shell completion success without an
explicit process exit code remains unscored.

If the host supplies a normalized current-session trajectory document, merge
it before derivation:

```bash
python3 "$SKILL_DIR/scripts/derive-candidates.py" supplement \
  --remote "$RUN_DIR/trajectory-events.json" \
  --current "$RUN_DIR/current-session-events.json" \
  --out "$RUN_DIR/trajectory-events.merged.json"
mv "$RUN_DIR/trajectory-events.merged.json" "$RUN_DIR/trajectory-events.json"
```

This replaces an already-ingested matching current-session prefix and appends
the complete local trajectory. If the host cannot supply current-session
events, record that limitation in the summary rather than claiming live-session
parity.

### 4. Derive usage patterns and candidates

```bash
python3 "$SKILL_DIR/scripts/derive-candidates.py" derive \
  --in "$RUN_DIR/trajectory-events.json" \
  --out "$RUN_DIR/candidates.json" \
  --user "<current-user>" \
  --repository "$REPOSITORY" \
  --branch "$BRANCH" \
  --usage-threshold 3 \
  --success-threshold 0.7
```

Candidate ranking is `usage_count DESC`, then `success_rate DESC`. Do not ask
the model to rediscover fingerprints, counts, thresholds, or ranking.

### 5. Compare existing skills

Read `.github/skills/**/SKILL.md`. Prefer:

1. `improve_existing_skill` when one skill can absorb the workflow;
2. `merge_skills` when overlapping skills should become one;
3. `create_skill` for a distinct reusable workflow;
4. `hold_as_pattern_only` when no usable workflow exists.

For an explicit Forge invocation, use `hold_as_pattern_only` only when the
session evidence has no identifiable reusable workflow. Failed sessions may
still produce recovery-oriented skills when the failure evidence is useful.

### 6. Author the skill change

Read:

- `$RUN_DIR/candidates.json`;
- `prompts/author-skill.md`;
- the relevant existing repository skills;
- representative session evidence needed to ground C/R/π/T.

Then make exactly one decision:

```text
create_skill | improve_existing_skill | merge_skills | hold_as_pattern_only
```

For create, improve, or merge, make the actual repository file changes under
`.github/skills/**` in this turn. Do not return an inline-only draft.

### 7. Validate promoted skills

Validate every created or changed `SKILL.md`:

```bash
python3 "$SKILL_DIR/scripts/validate-skill.py" \
  ".github/skills/<skill-name>/SKILL.md"
```

If validation fails, revise once. If the artifact still fails validation,
revert only the invalid Forge-generated change and use `hold_as_pattern_only`.

### 8. Write the run summary

Write `$RUN_DIR/forge-summary.md` with:

- scope and thresholds;
- evidence and candidate counts;
- selected decision and rationale;
- changed skill paths;
- validation results;
- gaps, uncertainty, and no-effect reason when held.

## Termination (T)

Success requires:

- evidence was scoped to the invoking user, current repository, and current branch;
- deterministic artifacts were produced;
- exactly one Forge decision was recorded;
- create, improve, and merge decisions produced valid `.github/skills` changes;
- hold decisions produced no repository skill changes;
- `$RUN_DIR/forge-summary.md` identifies every artifact and changed path.

Failure is explicit when evidence cannot be fetched, normalization fails,
candidate JSON is invalid, or a promoted skill cannot pass validation. Surface
the failing stage and stop; do not fabricate evidence or report success.

## Always do

- Keep the initial scope at user + repository + branch.
- Use the latest 100 matching sessions by default.
- Preserve the `3` usage and `0.7` success thresholds.
- Use deterministic scripts before semantic authoring.
- Compare against all existing repository skills.
- Prefer parameterization, improvement, and merging over duplicates.
- Let the surrounding runtime or Dreaming delivery layer handle human review.

## Never do

- Never expand to all repository users without an explicit repository-scope mode.
- Never infer process success from tool completion alone.
- Never create a skill from invented commands or unsupported workflow details.
- Never commit, push, or open a pull request as part of this skill.
- Never write Forge scratch artifacts into the target checkout.

## Gotchas / edge cases

- Remote ingestion may not yet contain the current live session.
- `apply_patch` updates cannot reconstruct a complete modified script; only added
  script files provide full cache content.
- Commands without cached script content do not become current Forge usage patterns.
- Two executions with equivalent script structure intentionally share a fingerprint.
- A successful explicit invocation can still produce `hold_as_pattern_only`.
- The generated query uses a 100-year time predicate because generic
  `session_store_sql` queries require a time bound. The effective selection
  remains the latest 100 repository/branch sessions.

## Assets and scripts

- `scripts/fetch-session-evidence.sh`: generate the scoped remote query and normalize rows.
- `scripts/derive-candidates.py`: normalize remote events, derive patterns, and rank candidates.
- `scripts/validate-skill.py`: validate promoted repository skill structure.
- `assets/schemas.json`: machine-readable artifact contracts.
- `prompts/author-skill.md`: semantic authoring and portfolio decision policy.

## Scope boundaries

This version intentionally matches Forge's user + repository + branch scope.
Repository-wide, multi-user Dreaming is not part of this package version.
Scheduling, artifact egress, proposal persistence, and review UI remain host
responsibilities.
