---
deferred_work_file: '{implementation_artifacts}/deferred-work.md'
---

# Step 5: Finalize

Terminal step. No commit, no push, no editor — the orchestrator creates the commit. `result.json` is the last artifact you write; the only thing that may follow it is the `On Complete` hook below, which is your final action before ending the turn.

In skip-review mode (`$BMAD_AUTO_SKIP_REVIEW=1`) the inline triple-review already ran in step-04 — do **not** re-run it here. In default mode you arrived here straight from step-03, and the orchestrator will review in a separate session.

## INSTRUCTIONS

1. **Tasks complete.** Verify every task in `{spec_file}`'s `## Tasks & Acceptance` is marked `[x]`. If any are not, go back and finish them first.

2. **Run verification.** Execute every command in the spec's `## Verification` section (skip only if the spec has no Verification section). A checked-off task list is a claim; passing commands are evidence — the orchestrator runs its own deterministic gates next, so a failure you skip here just burns a retry. If a command fails, fix the code and re-run until it passes. If you cannot make it pass without violating the frozen intent, escalate `CRITICAL` (`type: verification-failure`) instead of finalizing.

3. **Final status.** Set the spec frontmatter `status:`:
   - `done` when `$BMAD_AUTO_SKIP_REVIEW=1` (no separate review session follows; the inline triple-review already ran in step-04).
   - `in-review` otherwise (the orchestrator runs review in a fresh context).
   - In a repair session against an already-`done` spec, leave it `done`.

4. **Sprint sync / deferred-work update.**
   - **Not bundle mode:** follow `./sync-sprint-status.md` with `{target_status}` = `done` when `$BMAD_AUTO_SKIP_REVIEW=1`, else `review`.
   - **Bundle mode** (`{story_key}` starts with `dw-`): no sprint-status entry — skip the sync. Instead, for EACH id in `{dw_ids}`, set its `deferred-work.md` entry `status:` to `done {date}` and add `resolution: <one line: what was built>` directly after it (see `./deferred-work-format.md`). The orchestrator verifies these on disk — an unmarked entry fails the gate.

5. **Write `{result_file}`** (`$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/result.json`) using the Result Schema in `SKILL.md`:
   - `workflow` = `"auto-dev"` (fixed; the orchestrator rejects any other value).
   - `story_key` = `{story_key}` or `null` if unset.
   - `spec_file` = absolute path to `{spec_file}`.
   - `baseline_commit` = the value from the spec frontmatter.
   - `status` = the spec status set in instruction 3 (`done` / `in-review`), or `blocked` if you are finalizing after an escalation.
   - `tasks_total` / `tasks_done` = counts from `## Tasks & Acceptance`.
   - `verification` = one `{"command": "<cmd>", "ok": <bool>}` per command run in instruction 2 (else empty).
   - `escalations` = contents of any escalations raised this run (else empty).
   - **Bundle mode only:** include `"dw_ids": [<the bundle's ids, verbatim>]`.

6. **Run the On Complete hook** (see below). This is the only step that may follow writing `result.json`.

7. **End the turn** with a one-line statement of what was implemented. Do not ask questions, offer next steps, or wait for anything.

## On Complete

Run: `python3 {project-root}/_bmad/scripts/resolve_customization.py --skill {skill-root} --key workflow.on_complete`

If the resolved `workflow.on_complete` is non-empty, follow it now, before ending the turn (instruction 7). If it is empty, there is nothing to run — proceed straight to ending the turn.
