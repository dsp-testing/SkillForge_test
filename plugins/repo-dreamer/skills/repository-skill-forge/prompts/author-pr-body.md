<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Repository Forge pull request body authoring

Author the final pull request body only after proposal selection and target-SHA
resolution. Use the selected proposal, its validated `SKILL.md`, corroborating
default-branch repository files, extraction coverage, review result, and the
exact `selection.marker`.

Write a standalone body that replaces the host repository's pull request
template. Do not append to or preserve template sections that do not apply to a
skill-only documentation change.

Use exactly these user-facing sections, in this order:

```markdown
## What this adds

## Why it matters

## What changes in practice

## How to verify
```

## User-facing requirements

### What this adds

Open with one short, plain-English paragraph describing what Copilot will learn
to do. Do not begin with a file path or the words `skill`, `Forge`,
`forge-generated`, `proposal`, `candidate`, or `guardrail`.

### Why it matters

Describe the concrete repository problem in the maintainer's terms. Ground
every claim in a default-branch repository artifact or independently
corroborated sanitized evidence. Do not mention session IDs, candidate IDs, or
internal Forge mechanics here.

### What changes in practice

Include these exact labels:

```markdown
**Example request**

**Without these instructions**

**With these instructions**
```

Use an illustrative request that should activate the instructions. Contrast the
likely generic behavior with the specific repository workflow Copilot is now
directed to follow.

This is not an evaluation result. Do not invent command output, changed-file
counts, pass/fail statuses, commits, timings, or claims that a command caught a
specific error. Use conditional or directive language unless the result is
independently recorded in repository evidence.

### How to verify

Give a human reviewer something repository-relative that they can run or
inspect. For a documentation-only proposal, it is acceptable to ask the
reviewer to open the proposed `SKILL.md` and compare its commands with named
default-branch documentation. Never use `/tmp`, a home-directory path, a plugin
cache path, or another machine-specific command.

## Internal details

After the four user-facing sections, add:

```markdown
<details>
<summary>Forge details</summary>

- Proposal key: `...`
- Proposal version: `...`
- Decision: `...`
- Candidate IDs: `...`
- Confidence: ...
- Evidence window: ...
- Repository sources: `...`
- Target SHA: `...`
- Extraction: complete
- Validation: ...
- Review findings: ...
- Trusted-user diversity: unknown

</details>
```

For partial extraction, also include the discovered and completed session
counts, discovery completeness, known coverage or `unknown`, omission count and
kinds, and tool-event fallback status.

Place the exact `selection.marker` after the closing `</details>` tag as the
final non-whitespace content. Do not edit, reformat, or reconstruct it.

## Prohibited content

- `## Overview`, `## Checklist`, or `## Deployment`;
- unchecked checklist items;
- rollout, canary, observability, Kubernetes, or infrastructure template text
  that is unrelated to the proposed instructions;
- a `/tmp/copilot-plugins/...` validation command;
- raw session content or machine-specific paths;
- measured with/without-instructions evaluation claims. Evaluation is a
  separate future workflow.

Keep the visible user-facing portion under 500 words.
