---
deferred_work_file: '{implementation_artifacts}/deferred-work.md'
specLoopIteration: 1
---

# Step 4: Inline Triple-Review (skip-review mode only)

This step runs **only when `$BMAD_AUTO_SKIP_REVIEW=1`** — the orchestrator runs no separate review session, so this session is the sole quality gate and reviews its own work. A session that planned and implemented the change is a better-informed judge of it than a fresh reviewer. When `$BMAD_AUTO_SKIP_REVIEW` is unset, step-03 skips this step and the orchestrator reviews the work in a separate fresh-context session.

## RULES

- Review sub-agents get NO conversation context, and run at the same model capability as this session.
- Sub-agents are pre-authorized. If sub-agents are unavailable, run the three reviewers inline yourself — **never** generate prompt files and **never** HALT for a human.
- Read-only inspection: do NOT `git add` anything.

## INSTRUCTIONS

Set `{spec_file}` status to `in-review` in the frontmatter before continuing.

### Construct the diff

Read `baseline_commit` from `{spec_file}` frontmatter. If it is missing or `NO_VCS`, determine what changed best-effort; otherwise construct `{diff_output}` covering all changes — tracked and untracked — since `baseline_commit`.

### Review — three adversarial reviewers, no shared context

- **Blind hunter** — receives inline `{diff_output}` only (no spec, no docs, no project access). Invoke via the `bmad-review-adversarial-general` skill.
- **Edge case hunter** — receives `{diff_output}` plus read access to the project. Invoke via the `bmad-review-edge-case-hunter` skill.
- **Acceptance auditor** — receives `{diff_output}`, `{spec_file}`, and read access to the project; must also read the docs in `{spec_file}` frontmatter `context`. Checks for violations of the spec's acceptance criteria, rules, and principles.

### Classify

1. Deduplicate all findings.
2. Classify each. The first three are **this story's problem** (caused or exposed by the change); the last two are **not**:
   - **intent_gap** — caused by the change; unresolvable from the spec because the captured intent is incomplete. Do not infer intent unless exactly one reading is possible.
   - **bad_spec** — caused by the change (including direct spec deviations); the spec should have been clear enough to prevent it. When unsure between bad_spec and patch, prefer bad_spec — a spec-level fix yields more coherent code.
   - **patch** — caused by the change; trivially fixable without human input. Just part of the diff.
   - **defer** — pre-existing, surfaced incidentally. Collect for later.
   - **reject** — noise; drop. When unsure between defer and reject, prefer reject.
3. Resolve in cascading order. An intent_gap or bad_spec finding triggers a loopback — lower findings are moot because the code is re-derived. Increment `{specLoopIteration}` on each loopback; if it exceeds 5, escalate `CRITICAL` (`type: review-loop-exceeded`) and end the run.
   - **block-if triggered** — if review surfaces that a **Block If** condition from the spec's Boundaries was triggered by the change, revert the code changes, then escalate `CRITICAL` (`type: block-if`) and end the run. Do not decide it unattended.
   - **intent_gap** — root cause is inside `<frozen-after-approval>`. Revert the code changes, then escalate `CRITICAL` (`type: intent-gap`) and end the run. Do not infer intent.
   - **bad_spec** — root cause is outside `<frozen-after-approval>`. Extract KEEP instructions (what worked and must survive re-derivation), revert the code changes, amend the non-frozen spec sections that hold the root cause (respecting every constraint already logged in `## Spec Change Log`), and append a `## Spec Change Log` entry recording the triggering finding, what was amended, the known-bad state avoided, and the KEEP instructions. Then read fully and follow `./step-03-implement.md` to re-derive — this step runs again afterward.
   - **patch** — auto-fix directly. These are the only findings that survive loopbacks.
   - **defer** — append to `{deferred_work_file}` following `./deferred-work-format.md`.
   - **reject** — drop silently.

## NEXT

Read fully and follow `./step-05-finalize.md`.
