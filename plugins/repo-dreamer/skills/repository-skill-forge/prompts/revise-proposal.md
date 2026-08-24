<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Repository Forge proposal revision

Revise the current run-local proposed skill using only the development
evaluation failures supplied by `proposal-eval-controller.py`.

For every failed gate or case:

1. identify the missing, ambiguous, or harmful instruction that caused it;
2. revise the smallest relevant Conditions, Interface, Policy, Termination,
   asset, or reference section;
3. preserve behavior already supported by passing development cases;
4. keep repository-stable commands grounded in default-branch files;
5. remove over-specific guidance when it caused a regression or unnecessary
   token and tool use.

Do not read held-out results, edit evaluation cases, weaken thresholds, add
case-specific names or expected answers, reproduce raw session content, or
specialize the skill to the development prompts.

Write the revision into a new iteration directory. Validate it with
`validate-skill.py` and derive a new `proposalVersion` from the complete revised
proposal before recording the revision with the evaluation controller.
