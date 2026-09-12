# Te Varta repository instructions

## Source of truth

- Read `TEVARTA_MVP_BUILD_v2.1.md` before planning or implementing work. Read the sections relevant to the active task again before editing.
- The MVP document is frozen until Week 5 dogfooding evidence justifies a change. Do not edit it for preference, cleanup, or implementation convenience.
- Follow the active GitHub task for owner and scope. Every task must state an outcome, dependencies, "done means," and a verification method before implementation starts.
- Treat the API contract, scoring weights and thresholds, and anything on the deferred list as shared decisions. Do not change them without both founders' agreement in the Friday review.
- Keep the two product invariants intact: expensive work happens once globally, and an LLM may extract or phrase but may not own state, decide relevance, traverse the graph, set confidence, or generate arbitrary queries.

## Delivery loop

For each assigned task:

1. Inspect the current branch, working tree, relevant code, tests, and MVP sections. Preserve unrelated work.
2. Confirm the smallest runnable outcome and its acceptance checks. Flag a conflict with the frozen MVP instead of silently resolving it in code.
3. Use an installed skill when its documented scope matches the task. Read its `SKILL.md` first. Do not invoke skills ceremonially.
4. Implement a complete slice, including failure behavior, instrumentation, and fixtures where the MVP requires them.
5. Run the smallest meaningful checks, then any phase-required integration checks. A passing command is evidence only for what it exercised.
6. Review the final diff for scope, contract drift, secrets, unsafe data handling, missing authorization, and accidental edits.
7. Stage only the intended paths, commit, and push the completed task branch when the checks pass. Open or update a small PR for same-day review. Never force-push unless a founder explicitly asks.

If authentication, the network, or a failing unrelated baseline prevents a push, keep the verified local commit and report the exact blocker. Do not claim it was pushed.

## Model and review policy

- Use a balanced coding model for routine, bounded implementation.
- Use the strongest available reasoning model as a read-only advisor at phase start and phase close. The current recommendation, when available, is GPT-6 Astra with high reasoning.
- Escalate high-risk implementation—schema and migrations, graph propagation, event merge, ranking math, authentication/authorization, deletion, secrets, and production operations—to the strongest model before code is merged.
- Give the advisor a fresh review brief: relevant MVP sections, acceptance conditions, diff, test evidence, known tradeoffs, and unresolved risks. Ask for blocking findings first.
- The implementer resolves or explicitly disposition all blocking findings. The advisor does not commit, approve its own work, expand scope, or replace founder review.
- Curated graph edges always require human acceptance and two-person PR review.

## Git and commit discipline

- Use trunk-based development with small task branches and same-day PRs. Do not keep a branch open for an entire phase when work can land safely in smaller slices.
- Make each commit one reviewable outcome. Do not combine unrelated cleanup or user-owned changes.
- Stage explicit paths. Do not use `git add .` or another broad staging command in a non-clean working tree.
- Use an imperative subject such as `feat(phase-1): add ambiguous alias resolution`.
- Keep the body concise and include these headings when the change is non-trivial:

  ```text
  Problem:
  - What user, product, or engineering failure existed.

  Change:
  - What changed and why this approach was chosen.

  Improvement:
  - What is now measurably safer, faster, clearer, or possible.

  Verification:
  - Exact meaningful checks and their result.
  ```

- Never add `Co-authored-by` trailers or any Codex/AI attribution to commits, PRs, source files, or generated documentation.
- Do not create empty phase-complete commits. Close a phase through its merged task commits, phase verification evidence, advisor review, and a concise PR/project update.
- Commit and push authority does not include merging a PR or deploying to any environment. Those actions require their own explicit authorization.

## Phase gates

A phase may close only when:

- every required task satisfies its stated "done means" or is explicitly moved out by both founders;
- the phase demo works on the real integration path described in the MVP;
- required tests, lint, types, migrations, fixtures, and security checks pass at the merged revision;
- instrumentation ships with the behavior it measures;
- relevant decisions are recorded in `decisions.md`;
- the strongest-model advisor has reviewed the aggregate diff and evidence;
- blocking findings are resolved or accepted by both founders with a reason; and
- the remote branch and PR reflect the reviewed revision.

Use `docs/BUILD_WORKFLOW.md` for the task brief, advisor review, PR, and phase-close templates.

## Project-specific skill routing

- `software-delivery`: substantial phase or cross-component implementation.
- `graph-write`: every Neo4j read/write implementation; keep Cypher named and parameterized.
- `add-source`: adding or changing content feeds and their publisher/tier taxonomy.
- `curate-edge`: proposing evidence-backed graph edges; proposals still require human acceptance.
- `log-decision`: changes to contracts, formulas, thresholds, providers, or architecture.
- `brooks-review`: phase-close or PR maintainability review.
- `brooks-test`: review of an existing test suite's quality, not routine test creation.
- `ci-repair`: failing GitHub Actions checks when diagnosis and a fix are requested.
- `webapp-qa`: local web UI behavior; use the appropriate native iOS/Xcode checks for the app.
- `openai-docs`: current OpenAI model, API, prompting, or Codex guidance.

If no skill fits, proceed with the repository conventions and normal engineering judgment.
