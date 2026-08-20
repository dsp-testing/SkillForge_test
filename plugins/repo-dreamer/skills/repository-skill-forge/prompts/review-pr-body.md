<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Repository Forge pull request body review

Review the selected proposal's final pull request body against
`author-pr-body.md`, the validated `SKILL.md`, default-branch repository
artifacts, extraction coverage, target SHA, and `selection.marker`.

Block publication when the body:

- does not use the four required user-facing sections in order;
- opens with Forge jargon, a skill path, or unexplained internal terminology;
- preserves irrelevant host pull request template sections or unchecked boxes;
- contains a `/tmp`, home-directory, or plugin-cache verification path;
- invents command output, changed-file counts, pass/fail results, commits,
  timings, or an observed evaluation result;
- makes a repository claim that is not supported by the proposed instructions
  and corroborating repository evidence;
- hides partial extraction or other required limitations;
- exposes internal evidence before explaining the user-facing benefit;
- omits, duplicates, alters, or mismatches `selection.marker`.

Review the rendered body, not the authoring conversation. Any finding blocks
publication until the body is revised and validated again.
