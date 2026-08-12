<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Repository Forge independent proposal review

Review the proposed repository skill against its candidate metrics, sanitized
evidence, and default-branch repository behavior.

Also compare it with existing repository skills and all matching Forge pull
requests: open drafts, open ready-for-review PRs, closed-unmerged PRs, and
merged PRs. Reject duplicate or overlapping proposals.

Return JSON with:

```json
{
    "leakageFindingCount": 0,
    "unresolvedConflictCount": 0,
    "executable": true,
    "branchSpecific": false,
    "findings": []
}
```

Reject or revise the proposal when it:

- contains usernames, home paths, credentials, internal tokens, or
  machine-specific assumptions;
- reproduces raw session source instead of a parameterized workflow;
- depends on one unmerged branch without mainline corroboration;
- duplicates or conflicts with an existing repository skill;
- uses commands unsupported by repository evidence;
- lacks a concrete success check or recovery behavior;
- requires `gh` or independently authenticated GitHub access in Dreaming;
- claims multi-user evidence when trusted user identity is unavailable.
- assumes more than one proposal PR can be created or updated in a run;
- omits the required `skills-forge` label;
- describes partial extraction as complete or omits coverage and omission
  disclosure from a proposal published after a partial run.

Review the generated artifact, not the authoring conversation.
