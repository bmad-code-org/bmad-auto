# Releasing `bmad-auto`

One standardized flow. Cutting a release is: **pick a version → curate the
CHANGELOG → run `prepare` → open a PR → merge.** Everything mechanical (version
stamping, asset regeneration, tagging, the GitHub release) is automated, and the
tag + release are created automatically once the PR merges to `main`.

## TL;DR

```bash
git switch -c release/0.5.0                 # a release/feature branch (never main)
$EDITOR CHANGELOG.md                         # add a curated `## [0.5.0] — <date>` section
python scripts/release.py prepare 0.5.0      # stamp + regen assets (if TUI changed) + commit
git push -u origin release/0.5.0
gh pr create                                 # then: wait for green CI, merge
# → .github/workflows/release.yml tags v0.5.0 and publishes the GitHub release
```

## The flow in detail

### 1. Branch

Work on a `release/X.Y.Z` (or feature) branch. `prepare` refuses to run on `main`.

### 2. Curate the CHANGELOG

Add a section for the new version **before** running `prepare` — the engine
validates it exists and is non-empty. To see what shipped since the last tag:

```bash
python scripts/release.py commits     # commits since the last vX.Y.Z, grouped by type
```

Write the entry in the house style (see [CHANGELOG style](#changelog-style)).
Use today's date: `## [0.5.0] — YYYY-MM-DD`. You don't need to add the
`[0.5.0]: …/tag/v0.5.0` link reference at the bottom — `prepare` inserts it.

### 3. Prepare

```bash
python scripts/release.py prepare 0.5.0
```

This validates preconditions (clean-ish tree, branch ≠ main, tag absent, version
strictly greater than the current one, CHANGELOG section present), then:

1. inserts the CHANGELOG link reference if missing;
2. runs `scripts/sync_version.py 0.5.0` — stamps `__init__.py`, `pyproject.toml`,
   both `module.yaml` copies, `marketplace.json`, and re-locks `uv.lock`;
3. regenerates `docs/images/` screenshots + `demo.gif` **only if `src/automator/tui`
   changed since the last tag** (override with `--force-assets` / `--no-assets`);
4. runs `trunk fmt` if available (keeps the commit lint-clean);
5. commits everything as `chore(release): 0.5.0 — <summary>` (explicit paths only —
   it never `git add -A`).

Useful flags: `--dry-run` (report, mutate nothing), `--force-assets`, `--no-assets`,
`--allow-dirty` (proceed despite unrelated dirty files; still commits only known paths).

### 4. PR → merge → auto-publish

Push the branch, open a PR, wait for CI (`test`, `version-sync`, `lint`) to go green,
and merge. On the merge to `main`, **`.github/workflows/release.yml`** runs
`release.py publish`, which:

- no-ops if `vX.Y.Z` already exists (idempotent — non-release merges do nothing);
- otherwise creates the `vX.Y.Z` tag at the merge commit and a GitHub release whose
  notes are the CHANGELOG section for that version.

Nothing to do by hand after merge. If you ever need to publish manually (e.g. the
workflow was disabled), run `python scripts/release.py publish` on `main` yourself.

## CHANGELOG style

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), `### Added` /
`### Fixed` / `### Changed`. Entries must be **curated prose, never pasted commit
messages or raw info dumps**:

- Lead each entry with a **bolded subject**, then 1–2 tight sentences on the
  user-facing impact and the why — not the implementation diary.
- One entry per meaningful change; fold incidental commits into the relevant entry.
- Prefer concision. If an entry runs past a few sentences, it's probably a dump —
  trim it to the change and its consequence.

```markdown
## [0.5.0] — 2026-07-01

### Fixed

- **Cleanup no longer stops other projects' runs.** tmux sessions are global; a run
  id not found under the current project was treated as a prunable orphan and could
  match another project's live session. Sessions are now stamped with their project
  and cleanup only prunes its own.
```

## Local pre-flight

`python scripts/release.py check` mirrors the CI guards (version-sync + CHANGELOG
section present). Run a full `trunk check` (no path filter) before pushing — CI
lints prettier/markdownlint/yaml beyond ruff/black.
