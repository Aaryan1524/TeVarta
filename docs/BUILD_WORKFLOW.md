# Te Varta build workflow

This playbook turns the frozen MVP into small, reviewable deliveries for two founders. It complements `TEVARTA_MVP_BUILD_v2.1.md`; it does not revise product scope or architecture.

## 1. Phase start

The build-planning agent prepares the phase brief in GitHub Projects before implementation. Keep it short:

```text
Phase:
Outcome and demo:
MVP sections:

Tasks:
- [owner] outcome | dependencies | done means | verification

Frozen interfaces:
Top risks or unknowns:
Out of scope:
```

Then run a read-only advisor pass with the strongest available reasoning model. The advisor checks sequencing, missing dependencies, numerical assumptions, acceptance criteria, architecture invariants, security/data risks, and whether the demo measures the real product claim. It returns:

```text
Blocking findings:
Recommended improvements:
Accepted tradeoffs:
Revised task order, if any:
```

Apply improvements that clarify or de-risk the frozen MVP. Product changes wait for founder agreement; the advisor cannot unfreeze the spec.

## 2. Task loop

Each task should be small enough for same-day review and complete enough to run or observe.

1. Create a task branch from the latest `main`.
2. Read the task's MVP sections and inspect existing work before editing.
3. Add or update the fixture/contract first when another track depends on the interface.
4. Implement the behavior and meaningful failure paths.
5. Add instrumentation in the same task when the MVP relies on that measurement.
6. Run targeted checks and inspect the diff.
7. Commit with the problem, change, improvement, and verification summary.
8. Push the current branch and open or update a small PR for same-day review.

Preferred branch pattern: `phase-<n>/<owner>-<outcome>`.

## 3. Commit message

Use 1–2 bullets per heading. State observed improvements; do not claim performance, safety, or correctness that was not measured.

```text
feat(phase-1): add ambiguous alias resolution

Problem:
- Plain aliases such as Apple could represent multiple entities.

Change:
- Store aliases with a composite key and disambiguate using article context.

Improvement:
- Ambiguous names no longer resolve silently to an arbitrary entity.

Verification:
- Alias resolver tests pass for Apple and five SanDisk spellings.
```

Small mechanical commits may use only a clear subject. Never include AI attribution or a `Co-authored-by` trailer.

## 4. Pull request review

The human reviewer checks the diff and runs or validates the relevant evidence. Curated edges require two human eyes. A model review is additional evidence, not approval.

Merge only when:

- the task's "done means" is demonstrated;
- contract changes are intentional and fixtures agree;
- relevant checks pass on the PR revision;
- secrets and sensitive identifiers are absent from source and logs;
- instrumentation and failure behavior are present where required; and
- the PR contains no unrelated changes.

## 5. Phase close

Run integration checks on the aggregate merged revision, not only on individual task branches. Demo the exact Week 1–8 outcome defined in MVP section 14, then ask the strongest available model for a fresh read-only review of:

- the phase brief and relevant MVP sections;
- the diff from the previous phase checkpoint;
- test, migration, build, and demo evidence;
- open defects, decisions, and cost/security observations.

Resolve blocking findings or record a joint founder acceptance with a reason. Post this compact phase note to the project/closing PR:

```text
Phase <n> — <name>

Delivered:
- <observable outcome>

Improved:
- <user/product/engineering effect>

Verified:
- <command, demo, or metric and observed result>

Advisor review:
- <model + reasoning level + blocking findings resolved, or none>

Carryover:
- <owner + next action, or none>

Revision:
- <merged commit SHA and PR links>
```

Do not create an empty checkpoint commit. The merged task commits and the phase note are the audit trail. Confirm `main` and the remote point to the reviewed revision before starting the next phase.

## 6. Reasoning budget

Spend model capability where errors are expensive:

| Work | Default model posture |
|---|---|
| Task formatting, fixtures, bounded CRUD, routine tests | Balanced coding model, medium reasoning |
| Cross-component implementation and difficult debugging | Strong coding model, high reasoning |
| Schema/migrations, graph math, clustering/merge, auth, deletion, production ops | Strongest available model before merge |
| Phase plan and aggregate phase close | GPT-6 Astra (currently), high reasoning, fresh read-only advisor pass |

Increase reasoning only for a concrete risk or unresolved finding. Keep normal status updates, commit bodies, and phase notes concise.

Model availability depends on the account and runtime. Record the model actually used; if the preferred advisor is unavailable, use the strongest available alternative and state the substitution.

## 7. Conflict and recovery rules

- If implementation conflicts with the frozen MVP, stop that portion and surface the exact section and consequence.
- If checks fail, classify the failure before retrying: change defect, environment limitation, stale expectation, unrelated baseline, or flaky test.
- If a push fails, retain the verified local commit and report the branch, commit SHA, and exact failure.
- Never force-push, rewrite shared history, discard another person's working tree, or include unrelated files to make a phase look complete.
