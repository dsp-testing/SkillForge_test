<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Repository Forge subject clustering

Group only the supplied deterministic candidates into repository subject areas.
Do not inspect raw sessions and do not invent workflows.

Return JSON:

```json
{
    "clusters": [
        {
            "label": "short repository subject",
            "candidateIds": ["candidate-id"],
            "rationale": "why these candidates describe one reusable workflow"
        }
    ]
}
```

Rules:

- Assign every supplied candidate exactly once.
- Prefer path families, tool signatures, and repository concepts over branches.
- Do not cluster merely because candidates came from the same user, branch, or
  session.
- Keep unrelated workflows separate.
- A one-candidate cluster is valid.
- Do not reproduce command bodies or add information absent from the candidate
  document.
