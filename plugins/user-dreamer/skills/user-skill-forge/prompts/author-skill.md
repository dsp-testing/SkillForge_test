<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Forge authoring policy

Use the deterministic candidate document and source session evidence to make
exactly one portfolio decision:

```text
create_skill | improve_existing_skill | merge_skills | hold_as_pattern_only
```

## Decision order

1. Prefer `improve_existing_skill` when an installed skill can absorb the
   workflow through clearer policy, parameters, recovery, or validation.
2. Prefer `merge_skills` when multiple installed skills overlap and one
   parameterized skill would reduce ambiguity.
3. Use `create_skill` for a distinct, recurring workflow with a clear trigger,
   interface, ordered policy, and termination check.
4. Use `hold_as_pattern_only` only when no identifiable reusable workflow
   exists. For an explicit Forge invocation, uncertainty alone is not enough to
   hold; create a best-effort draft with the uncertainty recorded.

Failed sessions can support recovery-oriented skills. Never invent commands,
paths, outcomes, or requirements that are absent from evidence.

## Required repository behavior

For `create_skill`, `improve_existing_skill`, or `merge_skills`, make the actual
file changes under `.github/skills/**` in the current turn. The surrounding
runtime captures those changes for proposal review. Do not commit, push, open a
pull request, or substitute an inline chat draft for a repository artifact.

For `hold_as_pattern_only`, do not change repository skill files.

## Generated skill contract

Every created or changed skill must:

- use a kebab-case directory and matching frontmatter `name`;
- include a concise description stating what it does and when to use it;
- include `generated-by: forge-agent`;
- declare `**Abstraction level:** primitive|compositional|strategic`;
- encode these sections in order:
    1. `# <Skill Title>`
    2. `## Purpose`
    3. `## Conditions (C)`
    4. `## Interface (R)`
    5. `## Policy (π)`
    6. `## Termination (T)`
    7. `## Always do`
    8. `## Never do`
    9. `## Gotchas / edge cases`
    10. `## Assets and scripts`
    11. `## Scope boundaries`

Keep the body operational and below approximately 5000 tokens. Move reusable
executable logic into `scripts/` only when it is generalizable, parameterized,
and independently verifiable. Move bulky templates and examples into `assets/`.

## Evidence ledger

Before editing, map evidence to:

- **C**: trigger, non-trigger, assumptions, and boundaries;
- **R**: entry point, parameters, inputs, outputs, and artifacts;
- **π**: exact ordered procedure, branches, and recovery;
- **T**: success checks, failure checks, and response to failed verification.

Preserve exact commands and paths when evidence supports them. If a required
part of C/R/π/T cannot be grounded, record the gap and avoid inventing it.

## Portfolio safeguards

- Compare every candidate with `.github/skills/**/SKILL.md`.
- Prefer parameterization over near-duplicate variants.
- Treat four or more overlapping skills in one task area as a warning.
- Do not weaken correct existing behavior during improvement or merging.
- Validate every promoted `SKILL.md` before reporting success.

## Quality gate before promotion

Before choosing create, improve, or merge:

1. Simulate activation from only the proposed description and Conditions.
2. Confirm a fresh agent can follow Interface and Policy without the source transcript.
3. Confirm Termination distinguishes success from failure and gives a recovery action.
4. Compare the proposed result with the source evidence and preserve exact values.
5. Check nearby skills for trigger overlap, contradictory policy, and lost behavior.
6. For improvement or merge, identify the baseline behavior that must remain unchanged.
7. For domain-specific examples, preserve structure but derive values from the current task.

If the first validation fails, revise once. If the revised skill still fails,
remove only the invalid Forge change and use `hold_as_pattern_only`.
