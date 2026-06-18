---
name: bmad-release
description: "Development-only release driver for bmad-automator itself: curate the CHANGELOG for a target version, then bump + regenerate assets + commit on a branch ready for PR. Invoked as /bmad-release <version>. NOT a shipped module skill — never installed by bmad-auto init."
---

# bmad-release

Standardized release prep for the **bmad-automator repo itself**. You curate the
CHANGELOG entry (the judgement step) and drive the deterministic engine
(`scripts/release.py`). The engine handles version stamping, conditional asset
regeneration, tagging metadata, and the commit; the GitHub tag + release are then
created automatically by `.github/workflows/release.yml` when the PR merges to `main`.

Read `RELEASING.md` at the repo root for the full standard before you start.

> **Scope:** dev-only. This skill exists only to develop bmad-automator and must
> never be added to `MODULE_SKILLS`, `marketplace.json`, `module.yaml`, or
> `_bmad/module-help.csv`. A project that runs `bmad-auto init` never receives it.

## Inputs

- A target version `X.Y.Z` (from the user / `/bmad-release <version>`). If absent,
  ask. Confirm it follows the bump intent (patch/minor) for what shipped.

## Steps

### 1. Sanity-check the working state

- Confirm you are **not** on `main`: `git rev-parse --abbrev-ref HEAD`. If on `main`,
  ask the user to create/switch to a `release/X.Y.Z` branch first (offer to do it).
- Confirm the tree is otherwise clean (uncommitted feature work should already be
  committed). The only file you will leave dirty is `CHANGELOG.md`.

### 2. Gather what shipped

Run and read:

```bash
python scripts/release.py commits
```

This lists commits since the last tag, grouped by conventional-commit type — your
raw material. Read the actual diffs if a commit subject is unclear; the CHANGELOG
must describe real user-facing impact, not restate commit messages.

### 3. Curate the CHANGELOG entry

Edit `CHANGELOG.md`: add a `## [X.Y.Z] — <today's ISO date>` section above the
previous one, with `### Added` / `### Fixed` / `### Changed` as needed. Follow the
house style in `RELEASING.md`:

- Bolded subject lead + 1–2 tight sentences of user-facing impact and the why.
- One entry per meaningful change; fold incidental commits in.
- Concise — trim anything that reads like an implementation diary or info dump.

Do **not** add the `[X.Y.Z]:` link reference at the bottom; `prepare` inserts it.

### 4. Review gate

Show the user the curated section and the planned actions:

```bash
python scripts/release.py prepare X.Y.Z --dry-run
```

Summarize: the version delta, whether assets will regenerate (and why), and the
files that will be committed. **Wait for the user's OK** before mutating anything.

### 5. Prepare

On approval:

```bash
python scripts/release.py prepare X.Y.Z
```

This stamps the version everywhere (via `sync_version.py`), regenerates
screenshots/demo only if `src/automator/tui` changed since the last tag, runs
`trunk fmt`, and creates the `chore(release): X.Y.Z — …` commit.

### 6. Hand back

Run `git show --stat HEAD` and summarize the release commit for the user. Tell them
the remaining manual steps (you do **not** do these unless asked):

```bash
git push -u origin <branch>
gh pr create        # then wait for green CI and merge
```

Remind them: once merged to `main`, the Release workflow auto-creates the `vX.Y.Z`
tag and GitHub release from the CHANGELOG — nothing else to do by hand.

## Notes

- Never create the tag or GitHub release yourself — that is the merge-triggered
  workflow's job (tags must point at the merged `main` commit).
- If `prepare` fails a precondition, fix the cause (usually a missing/empty
  CHANGELOG section or a stale version) rather than forcing past it.
- Run a full `trunk check` (no path filter) before pushing if you push on the
  user's behalf.
