<!-- Copyright (c) Microsoft Corporation. All rights reserved. -->

# Proposal inner-loop evaluation

Evaluate only the provisionally selected promoted proposal. Hold decisions do
not run evaluation. Raw session text, Vally trajectories, generated workspaces,
and grader evidence remain inside the isolated run directory.

## Build session-grounded cases

Start from the selected proposal's supporting evidence. Use exact supporting
session IDs only while the run is active to retrieve the minimum task intent,
repository starting point, relevant constraints, and externally verifiable
outcome needed for an evaluation case. Do not use unrelated sessions.

Write `$RUN_DIR/proposals/$PROPOSAL_KEY/evaluation-cases.json`:

```json
{
  "cases": [
    {
      "caseId": "stable-case-id",
      "sessionHash": "repository-salted-session-hash",
      "prompt": "A sanitized task that can be attempted from a clean checkout.",
      "rubric": [
        "The generated change reaches the repository-observable outcome.",
        "Existing repository behavior remains compatible."
      ],
      "environment": {}
    }
  ]
}
```

Prefer tests, merged-PR behavior, stable file contracts, and documented
repository commands as rubrics. Do not score similarity to the historical
assistant response. A case must not contain raw commands, secrets, usernames,
home paths, or machine-specific state. At least three distinct supporting
sessions are required so authoring, development, and held-out partitions are
all non-empty.

## Initialize

```bash
python3 "$SKILL_DIR/scripts/proposal-eval-controller.py" init \
  --cases "$RUN_DIR/proposals/$PROPOSAL_KEY/evaluation-cases.json" \
  --proposal-key "$PROPOSAL_KEY" \
  --proposal-version "$PROPOSAL_VERSION" \
  --skill-path "$GENERATED_SKILL_PATH" \
  --run-dir "$RUN_DIR/proposals/$PROPOSAL_KEY" \
  --max-iterations 3 \
  --runs 3 \
  --out "$RUN_DIR/proposals/$PROPOSAL_KEY/evaluation-state.json"
```

The controller deterministically partitions session-grounded cases into:

- `authoring`: may inform the initial proposal;
- `development`: may produce failure feedback for revisions;
- `heldout`: used once for final acceptance and never for revision.

The controller emits JSON-form YAML Vally specs. The same spec, model,
repository checkout, permissions, and runtime configuration must be used for
both arms. Baseline omits the proposed skill. Treatment loads only the exact
proposed skill directory.

## Execute actions

Request one action at a time:

```bash
python3 "$SKILL_DIR/scripts/proposal-eval-controller.py" next \
  --state "$RUN_DIR/proposals/$PROPOSAL_KEY/evaluation-state.json" \
  --out "$RUN_DIR/proposals/$PROPOSAL_KEY/evaluation-action.json"
```

For `run-vally`, execute Vally in an isolated temporary checkout outside the
target repository. Do not expose credentials or run generated code without the
host's approved containment. Use `--workers 1` unless the environment is known
to isolate concurrent trials. Record the same model and Vally version for both
arms.

Normalize Vally output to the `proposalEvaluationResult` schema:

```json
{
  "engine": "vally",
  "runCount": 3,
  "cases": [
    {
      "caseId": "stable-case-id",
      "score": 0.9,
      "passed": true,
      "evidence": []
    }
  ],
  "metrics": {
    "errorRate": 0.0,
    "scoreStdDev": 0.04,
    "meanTokens": 1200,
    "meanToolCalls": 8,
    "meanWallTimeMs": 42000
  }
}
```

Record it:

```bash
python3 "$SKILL_DIR/scripts/proposal-eval-controller.py" record \
  --state "$RUN_DIR/proposals/$PROPOSAL_KEY/evaluation-state.json" \
  --split development \
  --arm baseline \
  --result "$NORMALIZED_RESULT"
```

Use the action's exact `split` and `arm`.

## Revise

For `revise-proposal`, use `prompts/revise-proposal.md`. Write a new run-local
skill revision without modifying the prior revision, validate it with
`validate-skill.py`, derive a new `proposalVersion`, then record it:

```bash
python3 "$SKILL_DIR/scripts/proposal-eval-controller.py" revise \
  --state "$RUN_DIR/proposals/$PROPOSAL_KEY/evaluation-state.json" \
  --skill-path "$REVISED_SKILL_PATH" \
  --proposal-version "$REVISED_PROPOSAL_VERSION"
```

Never edit evaluation cases after initialization. Development baseline results
may be reused because that arm never loads the proposal. Every changed skill
revision requires new treatment runs. Held-out failures reject the proposal;
they must not trigger another revision.

## Acceptance

Default gates require:

- treatment mean score at least `0.8`;
- treatment improvement over baseline at least `0.05`;
- zero baseline-pass/treatment-fail case regressions;
- no error-rate increase;
- treatment score standard deviation no greater than `0.15`;
- token and tool-call increases no greater than `50%`.

After terminal status, write the compact summary:

```bash
python3 "$SKILL_DIR/scripts/proposal-eval-controller.py" status \
  --state "$RUN_DIR/proposals/$PROPOSAL_KEY/evaluation-state.json" \
  --assert-terminal \
  --out "$RUN_DIR/proposals/$PROPOSAL_KEY/evaluation-summary.json"
```

Copy that summary into the proposal's `evaluation` field. Only `accepted`
summaries with a passing held-out assessment satisfy proposal validation.
Discard the detailed evaluation directory after the run.
