---
---

# Step 3: Implement

Implement the spec. No push, no remote ops, sequential execution only. Content inside `<frozen-after-approval>` in `{spec_file}` is read-only — do not modify it. If any **Block If** condition in the spec's Boundaries triggers during implementation, escalate `CRITICAL` (`type: block-if`) and end the run — do not resolve it unattended.

## PRECONDITION

Verify `{spec_file}` resolves to a non-empty file on disk. If missing or empty, escalate `CRITICAL` (`type: missing-spec`) and end the run.

## INSTRUCTIONS

1. **Baseline.** If the spec frontmatter has no `baseline_commit` yet, capture it now: the full hash from `git rev-parse HEAD` (never `--short`), or `NO_VCS` if version control is unavailable. In a repair session against an already-`done` spec the baseline is already set — keep it.

2. **Status.** Set the spec frontmatter `status:` to `in-progress`, **unless** this is a repair session against an already-`done` spec (leave it `done`).

3. **Sprint sync.** If not bundle mode, follow `./sync-sprint-status.md` with `{target_status}` = `in-progress`. (The sub-step never regresses status and skips `dw-` keys, so it is a safe no-op in repair/bundle cases.)

4. **Load context.** If `{spec_file}` has a non-empty `context:` frontmatter list, load those files before implementing. When handing to a sub-agent, include them in its prompt.

5. **Implement.** Work the spec's `## Tasks & Acceptance` directly or via sub-agents (pre-authorized). In bundle mode, implement every `{dw_ids}` item as the one cohesive goal — never split; if an item cannot be done safely, escalate `CRITICAL` (`type: bundle-item-blocked`).

   **Path formatting:** markdown links written into `{spec_file}` use paths relative to the spec's directory; file paths in terminal output use CWD-relative `path:line` form (e.g. `src/path/file.ts:42`). No leading `/` in either case.

6. **Self-check.** Mark every completed task in `## Tasks & Acceptance` as `[x]`. If any task remains incomplete, finish it before continuing — an incomplete task list fails the orchestrator's verification and burns a retry.

## NEXT

- If `$BMAD_AUTO_SKIP_REVIEW=1`: the orchestrator runs no separate review session — read fully and follow `./step-04-review.md` to run the inline triple-review, then finalize.
- Otherwise: skip the inline review (the orchestrator reviews in a fresh session) — read fully and follow `./step-05-finalize.md`.
