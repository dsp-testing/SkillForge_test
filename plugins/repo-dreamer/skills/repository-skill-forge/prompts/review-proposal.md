<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Repository Forge independent proposal review

Review the proposed repository skill against its candidate metrics, sanitized
evidence, and default-branch repository behavior.

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

Review the generated artifact, not the authoring conversation.
