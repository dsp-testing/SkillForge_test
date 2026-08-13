<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Repository Forge proposal authoring

Use one eligible subject cluster, its deterministic metrics, the default-branch
repository skills and instructions, and the sanitized evidence ledger to make
one decision:

```text
create_skill | improve_existing_skill | merge_skills | hold_as_pattern_only
```

Prefer improvement and merging over duplicate skills. Author a repository skill
only when Conditions, Interface, Policy, and Termination can be grounded in the
evidence.

Assign the proposal a stable `proposalKey` derived from the intended skill name
or the existing skill being improved. The key must remain stable when cluster
membership or candidate IDs change. Include every cluster candidate ID in the
proposal manifest's `candidateIds` array.
Set `proposalVersion` from the complete proposal, including the sorted candidate
IDs and versions plus the generated skill content, so materially changed
evidence or instructions produce a new version.

Copy the extraction coverage into the proposal manifest. Use
`extraction.status: complete` for complete runs. For partial runs include
discovered/completed session counts, whether discovery completed, the coverage
ratio when known or an explicit unknown value otherwise, omission count and
kinds, and whether targeted fallback was enabled.

Never paste source session scripts into the proposed skill. Re-synthesize the
workflow as parameterized instructions and preserve exact commands only when
they are repository-stable, non-secret, and independently corroborated by
default-branch files or multiple sanitized evidence records.

For a promoted proposal:

- write under `$RUN_DIR/proposals/<proposal-key>/<skill-name>/SKILL.md`;
- include `generated-by: forge-agent`;
- include the required C/R/pi/T sections accepted by `validate-skill.py`;
- cite candidate IDs and repository artifacts in the proposal summary, not in
  generated instructions where they would distract future execution;
- assign a deterministic non-negative queue `rank`;
- keep unrelated subjects as separate queue entries;
- do not modify the target checkout or publish the proposal.

For a hold decision, write no `SKILL.md` and state the exact missing evidence or
safety condition.
