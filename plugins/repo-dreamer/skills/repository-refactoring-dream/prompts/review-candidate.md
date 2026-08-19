<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Repository Refactoring Dream independent review

Review the generated refactoring patch, repository instructions, baseline
behavior, and validation results. Review the artifact rather than the authoring
conversation.

Return JSON. `decision` must be exactly one of `accept`, `revise`, or `reject`.

```json
{
  "decision": "accept",
  "behaviorPreserved": true,
  "singleConcern": true,
  "simplificationIsReal": true,
  "testsAdequate": true,
  "findings": []
}
```

Reject the proposal when:

- observable behavior, public APIs, error semantics, ordering, concurrency,
  cancellation, logging, telemetry, or serialized formats changed;
- assertions were weakened or meaningful coverage was removed;
- complexity was moved rather than reduced;
- a speculative abstraction replaced small incidental duplication;
- an existing repository abstraction was overlooked;
- the patch mixes unrelated cleanup or exceeds a focused review scope;
- the justification relies only on line count, file length, or a generic code
  smell;
- validation is incomplete or the baseline was not established;
- the change conflicts with repository instructions or active work.

Require the final PR explanation to identify the concrete problem, preserved
invariants, validation commands, measured simplification, remaining risk, and
rollback path.
