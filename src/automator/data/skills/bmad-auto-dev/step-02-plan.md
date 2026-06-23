---
deferred_work_file: '{implementation_artifacts}/deferred-work.md'
---

# Step 2: Plan

Turn the intent into a "Ready for Development" spec at `{spec_file}`. No intermediate approvals — self-review stands in for the human checkpoint.

## INSTRUCTIONS

1. **Draft resume check.** If `{spec_file}` exists with `status: draft`, read it and capture the verbatim `<frozen-after-approval>...</frozen-after-approval>` block as `{preserved_intent}`. Otherwise `{preserved_intent}` is empty.

2. **Load planning context.**

   **Story mode** (`{story_key}` is set and does not start with `dw-`) — Epic story path:
   1. **Check for a valid cached epic context.** Look for `{implementation_artifacts}/epic-{epic_num}-context.md`. It is **valid** when it exists, is non-empty, starts with `# Epic {epic_num} Context:`, and no file in `{planning_artifacts}` is newer.
      - Valid → load it as the primary planning context; do not load raw planning docs. Go to step 2.3.
      - Missing/empty/invalid → compile it (step 2.2).
   2. **Compile epic context** by following `./compile-epic-context.md` — preferred via a sub-agent (pass the epic number, the epics file path, `{planning_artifacts}`, and output path `{implementation_artifacts}/epic-{epic_num}-context.md`); inline fallback if sub-agents are unavailable or the spawn fails. Then verify the output exists, is non-empty, and starts with `# Epic {epic_num} Context:`. If verification fails, escalate `CRITICAL` (`type: epic-context-failure`) and end the run. Otherwise load it.
   3. **Previous-story continuity.** Scan `{implementation_artifacts}` for specs in the same epic with a lower `{story_num}`. Load the most recent `done` spec (highest story number below current) and extract its Code Map, Design Notes, Spec Change Log, and task list as continuity context. If no `done` spec exists but an `in-review` one does for a lower story, load it as context too (no human to ask — proceed).

   **Bundle mode** (`{story_key}` starts with `dw-`): skip epic context and continuity entirely. The bundle file's intent and ledger entries are your planning context.

3. **Investigate the codebase** and any relevant context files. Isolate deep exploration in sub-agents where available; instruct them to return distilled summaries only, to avoid context snowballing.

4. **Write the spec.** Read `./spec-template.md` fully, fill it from the intent and investigation, and write `{spec_file}`. If `{preserved_intent}` is non-empty, substitute it for the template's `<frozen-after-approval>` block before writing. When filling **Boundaries & Constraints**, populate the **Block If** tier with any decisions surfaced during investigation that cannot be made safely without a human (these become CRITICAL block triggers in steps 3–4); omit the tier if there are none.

5. **Self-review** the spec against the READY FOR DEVELOPMENT standard (actionable, logical, testable, complete) and fix anything it surfaces.

6. **Intent gap.** If the intent is still unclear after investigation, do not fantasize requirements — escalate `CRITICAL` (`type: intent-gap`) and end the run.

7. **Scope split.** If the scope is genuinely multi-goal (see SCOPE STANDARD) or the spec exceeds 4,000 tokens:
   - Keep the first/primary goal in `{spec_file}`.
   - Append each deferred secondary goal to `{deferred_work_file}` following `./deferred-work-format.md`.
   - Regenerate `{spec_file}` for the narrowed scope (do not surgically carve sections out).
   - **Bundle mode never splits** — implement every `{dw_ids}` item as one cohesive goal. If an item cannot be specced safely, escalate `CRITICAL` (`type: bundle-item-blocked`).

8. **Re-read `{spec_file}` from disk.** If it is missing or empty, escalate `CRITICAL` (`type: spec-write-failure`) and end the run.

9. **Approve.** Set the frontmatter `status:` to `ready-for-dev`. Everything inside `<frozen-after-approval>` is now locked.

## NEXT

Read fully and follow `./step-03-implement.md`.
