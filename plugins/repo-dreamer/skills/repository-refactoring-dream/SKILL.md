---
name: repository-refactoring-dream
description: Find one high-confidence opportunity to simplify recently changed repository code and publish a small behavior-preserving refactoring PR.
user-invocable: true
---

# Repository Refactoring Dream

Review recently changed code in its repository context, select at most one
high-confidence simplification, and create a small pull request that preserves
observable behavior.

This POC focuses on reducing:

- unnecessary or overlapping state;
- similar logic that should reuse an existing abstraction;
- needlessly complex control flow;
- long functions or files with a clear separation boundary.

Refactoring is broader than simplification. Do not use this POC for API
redesigns, architectural rewrites, performance work, dependency migrations, or
style-only cleanup.

## Conditions

Use this skill only when:

- the target repository is checked out on a clean working tree;
- the default branch and a recent-change window can be identified;
- repository instructions and validation commands are available;
- the repository has tests covering the selected behavior, or characterization
  tests can be added before the refactor;
- GitHub pull requests can be read and created through approved tools.

Do not modify generated code, vendored code, lockfiles, snapshots, migrations,
or code owned by an active unmerged refactoring PR.

## Interface

Inputs:

- `repository`: exact `owner/name`;
- `windowDays`: default `7`;
- `maxChangedFiles`: default `100`;
- `maxDiffLines`: default `400`;
- `baseBranch`: repository default branch;
- `candidatePath`: optional path supplied by the caller;
- `dryRun`: default `false`.

The POC is stateless. Before selecting work, search open and recently closed
pull requests for prior Refactoring Dream proposals and semantically related
refactors. Do not repeat a rejected or already represented proposal.

## Policy

### 1. Establish repository rules and recent scope

Read repository instructions, contributor guidance, build configuration,
validation commands, ownership rules, and relevant architecture documentation.
These sources override generic clean-code preferences.

Inspect files changed on the default branch during `windowDays`. If
`candidatePath` is supplied, restrict discovery to that path but still inspect
its callers, callees, tests, and existing repository abstractions.

Exclude:

- mechanical formatting-only changes;
- generated, vendored, snapshot, migration, and lock files;
- files currently being substantially modified by an open PR;
- code without a practical behavior-preservation check;
- changes that would exceed `maxChangedFiles` or `maxDiffLines`.

### 2. Find candidates in repository context

Inspect the recent code together with surrounding modules and repository-wide
prior art. Search before proposing a new helper, type, state variable, or
abstraction.

Candidates must match one primary category:

1. `remove_unnecessary_state`
   - derived values stored as mutable state;
   - multiple flags representing one state machine;
   - duplicated caches, queues, or lifecycle tracking;
   - state that can be computed safely from an authoritative source.
2. `reuse_existing_abstraction`
   - recent code duplicates or nearly duplicates an existing helper;
   - parallel implementations differ only in parameters or representation;
   - consolidation reduces maintenance without creating a speculative
     abstraction.
3. `simplify_control_flow`
   - avoidable nesting, branching, or duplicated exit paths;
   - complex orchestration that can become a clear sequence of named steps;
   - conditionals made unnecessary by an existing type or invariant.
4. `extract_cohesive_unit`
   - a long function or file contains a cohesive unit with a stable boundary;
   - extraction improves navigation and review without increasing coupling.

Code smells are investigation signals, not sufficient evidence. Do not select a
candidate solely because a metric threshold is exceeded.

### 3. Rank and gate candidates

For every candidate, record:

- exact files and symbols;
- the concrete maintenance problem;
- repository evidence showing the problem is real;
- the existing abstraction or invariant that enables simplification;
- the behavior that must remain unchanged;
- relevant tests and validation commands;
- expected diff size and review burden;
- uncertainty and regression risks.

Select at most one candidate. Prefer candidates that:

- are localized and independently reviewable;
- remove concepts, branches, or state rather than add abstractions;
- are supported by existing repository conventions;
- have strong test coverage;
- have a clear before-and-after explanation;
- can be reverted independently.

Reject a candidate when it:

- changes observable behavior or a public contract;
- requires guessing product intent;
- adds a speculative abstraction to remove small incidental duplication;
- crosses unrelated subsystems;
- depends on broad renaming or file movement;
- is primarily aesthetic;
- lacks adequate tests;
- conflicts with active work;
- cannot fit in one focused PR.

If no candidate passes every gate, finish with `NO_PROPOSAL`.

### 4. Establish the behavior baseline

Before editing:

1. identify the smallest tests that exercise the selected behavior;
2. run them against the unmodified checkout;
3. add focused characterization tests first if important behavior is uncovered;
4. record the baseline commands and results.

Do not proceed when the baseline is failing unless the failure is proven
unrelated and repository policy explicitly permits it. Never weaken, delete, or
rewrite assertions merely to make the refactor pass.

### 5. Apply one behavior-preserving refactor

Make the smallest coherent transformation. Preserve:

- public APIs and serialized formats;
- error types, messages, and timing when observable;
- ordering, concurrency, retries, and cancellation semantics;
- logging and telemetry semantics;
- performance characteristics where they are contractually or operationally
  significant;
- platform-specific behavior.

Prefer a sequence of small edits that keeps the repository buildable. Reuse
existing helpers before introducing new ones. Do not combine unrelated cleanup
with the selected refactor.

### 6. Validate independently

Run:

1. the focused baseline tests;
2. tests for affected callers and integrations;
3. repository formatting, lint, and type checks relevant to the changed files;
4. the smallest build that covers the change.

Then perform an independent review using `prompts/review-candidate.md`.

Reject and revert the proposal if validation:

- exposes a behavior change;
- requires test weakening;
- shows the simplification moved complexity elsewhere;
- increases coupling or public surface area without strong justification;
- cannot establish adequate confidence.

### 7. Publish one focused PR

When `dryRun=true`, report the candidate and proposed patch without publishing.

Otherwise create one PR containing:

- **Problem:** the concrete unnecessary state, duplication, control-flow
  complexity, or oversized unit;
- **Why now:** the recent change or repeated evidence that exposed it;
- **Refactor:** what was simplified and what was deliberately left unchanged;
- **Behavior preservation:** tests, invariants, and validation results;
- **Measured effect:** concepts, branches, state, duplication, or file/function
  size removed;
- **Risk:** remaining uncertainty and easy rollback instructions.

Include this marker exactly once:

```text
repository-refactoring-dream:v1
```

Keep the PR small and limited to one concern. Do not claim that reduced line
count alone proves improved maintainability.

## Termination

Finish with exactly one status:

- `PR_CREATED`: one validated refactoring PR was created;
- `DRY_RUN_PROPOSAL`: one validated proposal was prepared but not published;
- `NO_PROPOSAL`: no candidate passed all safety and usefulness gates;
- `BLOCKED`: required repository context, tests, validation, or publication
  capabilities were unavailable.

Report the inspected scope, selected category, validation performed, and the
reason for the final status. Never fabricate a candidate to ensure a daily PR.

## Follow-up learning

Review outcomes are research evidence:

- accepted changes may become repository instructions, lint rules,
  deterministic codemods, or reusable skills;
- rejected changes must record whether the cause was false-positive detection,
  behavior risk, weak tests, poor scope, or maintainer preference;
- repeated accepted patterns should move from agentic discovery toward
  deterministic prevention.

**Abstraction level:** strategic
