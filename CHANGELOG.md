# Changelog

All notable changes to `bmad-automator` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the project is pre-1.0,
breaking changes may land in a minor release.

## [0.5.0] — 2026-06-20

### Added

- **Plugin system.** New `automator.plugins` package — a general extension layer: a `plugin.toml`
  manifest (metadata, declarative `[hooks.<stage>]`, a `[[settings]]` schema, optional in-process
  `[python]`), a folder-drop loader with builtin/project overlay (and a locked seam for
  entry-point packaging later), a trust allowlist (`[plugins] enabled` in `policy.toml`), and a
  registry that isolates plugin failures. A dropped `[python]` plugin is never imported unless
  explicitly enabled. Plugins can **observe, veto (defer/pause/skip), and mutate** a shared
  context at every run/sweep lifecycle stage via the hook bus, with an O(1) no-op fast path so
  zero-plugin runs stay byte-identical.
- **Dynamic, TOML-driven settings.** The settings schema moves to `data/settings/core.toml`
  (presentation only; defaults/options referenced from the `policy.py` dataclasses, never
  duplicated), the TUI settings screen renders from a registry, and an enabled plugin's
  `[[settings]]` appear under `[plugins.<name>]`.
- **Workflow plugins.** A plugin can declare a `[workflows.<name>]` table that injects an extra
  agent session at a lifecycle stage (`post_dev_phase` / `post_review_result`, run by the `dev` or
  `review` adapter); the prompt substitutes `{story_key}`/`{run_id}`/`{scripts}`. Non-blocking by
  default (advisory); a blocking workflow that fails routes through the normal defer path. Ships
  with a worked-example plugin (`examples/plugins/guardrails/`) exercising every extension point and
  a full [plugin-authoring guide](docs/plugin-authoring-guide.md).

### Changed

- **The game-engine layer is now a plugin.** Unity runs entirely through the plugin system, with
  no engine-specific code in the core loop. Enable it with `[plugins] enabled = ["unity"]` and
  configure it under `[plugins.unity]` (`editor_mode`, `mcp`, `unity_path`, `ready_timeout_sec`,
  `ready_grace_sec`). Behavior — the readiness gate, `per_worktree` Editor setup/teardown, MCP
  agent routing, and Library priming — is unchanged.

### Deprecated

- The `[engine]` policy block is deprecated in favor of `[plugins] enabled = ["unity"]` +
  `[plugins.unity]`. Existing `[engine]` configs still load but emit a deprecation warning and are
  folded onto the `unity` plugin; explicit `[plugins.unity]` values win. `[engine]` will be
  removed in a future release.

## [0.4.4] — 2026-06-19

### Fixed

- Unity `per_worktree`: auto-recover merge-back when a competing Editor leaks asset writes
  (`.cs.meta` GUIDs, asmdef edits) into the **main** checkout. Previously git refused the merge
  pre-flight because the target already held the unit's incoming files as dirt, escalating the unit
  spuriously. Merge-back now cleans only the leaked copies of this branch's incoming files (journaled
  as `merge-target-cleaned`); dirt outside the branch's path set still escalates as possible operator
  work, with a distinct message.
- Unity `per_worktree`: route **every** worktree CLI's MCP config at the worktree's Editor, not
  just the dev agent. When dev and review use different CLIs (e.g. `dev=claude`, `review=codex`),
  the review agent could read a main-repo-seeded config and route its asset writes into the main
  checkout. Each agent's config is now written to the deterministic per-path port and verified; a
  mismatch fails the setup hook (the unit defers) instead of leaking writes.

### Changed

- Unity engine plugin: pin the `unity-mcp-cli` verification stamp to **v0.81.1** (subcommand
  signatures re-checked; no call-site changes). Documents the new upstream **dev-control HTTP
  bridge** (dev-only, off by default, not wired) in the [Game Engine MCP guide](docs/game-engine-mcp-guide.md).

## [0.4.3] — 2026-06-18

### Added

- **Game-engine plugin layer (opt-in; Unity).** New `[engine]` policy section adapts the
  dev/sweep cycle to projects that drive a live engine Editor through an MCP (Unity via
  [IvanMurzak/Unity-MCP](https://github.com/IvanMurzak/Unity-MCP) or
  [CoplayDev/unity-mcp](https://github.com/CoplayDev/unity-mcp)); off by default. Plugins ship
  like CLI profiles — bundled under `automator/data/engines/<name>/`, overridable in
  `.automator/engines/<name>/`. `editor_mode` couples to `[scm] isolation`: `shared` runs the
  agent in place on the operator's open Editor; `per_worktree` gives each unit its own managed
  Editor. A readiness gate blocks until the Editor + MCP report ready before each unit, deferring
  on timeout instead of starting against a half-open Editor.
- **Unity `per_worktree` mode.** Each unit runs in its own git worktree with a dedicated Editor:
  - Launches in local (Custom) mode — `bootstrap-local` plus `open --start-server true` so the
    Editor hosts its own per-path MCP server; this makes `wait-for-ready` a real readiness signal
    before any client connects. Connection knobs overridable via `BMAD_AUTO_UNITY_MCP_*`
    (`…_LOCAL=0` keeps the prior cloud launch).
  - Primes the worktree `Library` with a reflink/CoW copy of the warm main `Library`, so Unity
    reimports incrementally rather than cold — a cold import on a large project crashes the import
    workers (Burst `SIGFPE` writing `VirtualArtifacts`). Tunable via `BMAD_AUTO_UNITY_LIBRARY_SEED`
    and `…_SEED_MODE` (`reflink`|`copy`|`symlink`|`off`).
  - Teardown quits the Editor and reaps its child `gamedev-mcp-server` on completion or pause, so
    neither leaks across runs (a leaked server holds its port and breaks the next run).
  - Cold-launch grace via `[engine] ready_grace_sec`; MCP skill tree seeded into each worktree via
    `seed_globs`; `init` now gitignores `.automator/cache/`.
- **Worktree config seeding.** A fresh worktree checks out tracked files only, so a project's
  gitignored MCP/CLI configs (`.mcp.json`, `.claude/settings.json`, …) were missing — isolated
  sessions then timed out reaching their MCP and escalated as spurious spec errors. Each loaded
  adapter's configs are now copied in before launch, via new `[scm]` knobs `seed_adapter_defaults`
  (default on) and `worktree_seed` (extra paths). Both are in the TUI settings editor.
- **Game Engine settings in the TUI.** All six `[engine]` keys (`name`, `editor_mode`, `mcp`,
  `unity_path`, `ready_timeout_sec`, `ready_grace_sec`) are now editable in the settings editor
  (`g`) under a collapsible titled **Game Engine**; the `editor_mode` ↔ `[scm] isolation` coupling
  is validated on save. New authoring docs: [Writing a Game Engine plugin](docs/game-engine-plugin-guide.md)
  and [Writing a plugin for a specific Editor MCP](docs/game-engine-mcp-guide.md) (full
  `BMAD_AUTO_UNITY_*` env-var reference).

### Changed

- Default `limits.session_timeout_min` raised from 45 to 90 minutes — the old default cut off
  substantial units, especially MCP-driven Unity sessions where each Editor step is a slow
  round-trip. Override per project under `[limits]`.

### Fixed

- `bmad-auto cleanup` (and the TUI `c` action) no longer stops other projects' live runs. tmux
  sessions are global but were named only `bmad-auto-<run_id>`, so a run id absent from the current
  project looked like a prunable orphan and matched another project's active run. Sessions and
  windows are now stamped with their project (`@bmad_project`); cleanup prunes only the current
  project's, while still clearing true same-project orphans. Pre-existing untagged sessions are
  left untouched.

## [0.4.2] — 2026-06-17

### Fixed

- Answering sweep decisions over an attach now returns you to your terminal. After the
  last decision in a cycle was answered, the session previously stayed in the orchestrator
  window instead of handing control back. The sweep now returns the terminal as soon as the
  current cycle's decisions are answered and continues running bundles in the background.
  `bmad-auto attach` lands on the orchestrator window when a decision is pending and restores
  your previous session on exit.

## [0.4.1] — 2026-06-16

### Fixed

- Worktree isolation (`[scm] isolation = "worktree"`) now works. Isolated runs previously
  failed on the first session with `Unknown command: /bmad-auto-dev`. Worktrees are now
  created under the run directory (`.automator/runs/<run_id>/worktrees/<unit>`) instead of
  inside `.git/`, and each worktree is provisioned with the bundled skills and signal hook so
  project commands resolve correctly.

## [0.4.0] — 2026-06-16

First release with **opt-in git-worktree isolation** for runs and sweeps. The default is
unchanged: with no `[scm]` configuration, work happens in place on the checked-out branch
exactly as before (`isolation = "none"` is byte-for-byte identical to prior behavior).

### Added

- **Configurable `repo_root` + Workspace seam.** `_bmad/bmm/config.yaml` gains an optional
  `repo_root` key (defaults to the project dir) that decouples "where git work + code sessions
  happen" from "where run state lives." All code/git/artifact access now routes through a single
  `Workspace` indirection, so redirecting work into a worktree is a localized change rather than a
  sweep across the engine.
- **Worktree isolation** — `[scm] isolation = "worktree"` runs each story (and each sweep bundle)
  in its own `git worktree` on a dedicated `automator/<run_id>[/<story>]` branch cut from the
  target branch, then merges it back into the target **locally** (merge strategy `ff`, `merge`, or
  `squash`). The main checkout stays free while a run is in flight, and run state stays in the main
  repo's `.automator/` — never inside a worktree. Knobs: `branch_per` (`story` | `run`; `run`
  shares one branch across the run and forces `delete_branch = false`), `target_branch` (default =
  the branch checked out at run start; a configured branch is created/checked out in the main repo
  and never inside a worktree), `delete_branch`, and `keep_failed`.
- **Failed-unit forensics.** A deferred/escalated unit's full diff (tracked + untracked) is
  preserved to `run_dir/failed/<unit>/changes.patch`; with `keep_failed` (default) its worktree +
  branch stay mounted for inspection. `failed_diff_max_mb` (default `5`) caps the per-file size of
  untracked files in that patch — oversized files are skipped with a labelled marker — and
  `failed_diff_unlimited` lifts the cap entirely (logs a warning when active).
- **`commit_message_template`** — optional `[scm]` template (`{story_key}` / `{run_id}`
  substituted) used for story and sweep-bundle commits when set.
- The full `[scm]` section (isolation, `branch_per`, `target_branch`, `merge_strategy`,
  `delete_branch`, `keep_failed`, the failed-diff caps, and the commit template) is editable from
  the TUI settings screen. (`max_parallel` is omitted while it stays inert.)
- **Low-frame-rate TUI mode.** `bmad-auto tui --low-frame-rate` (or `[tui] low_frame_rate = true`,
  editable from the settings screen) caps Textual to 15fps and disables animations by setting
  `TEXTUAL_FPS` / `TEXTUAL_ANIMATIONS` before the app starts. Fixes the repaint tearing/garbage
  seen when driving the dashboard over a slow or high-latency link (SSH, Tailscale), where a 60fps
  update stream can't drain in time and partial frames paint over old ones. The setting takes
  effect the next time the TUI launches; an explicit `TEXTUAL_FPS` in the environment still wins.
- **git worktree / branch / merge / diff primitives** in `verify.py` (worktree add/remove/list/
  prune, `create_branch`, `merge_branch`, `capture_diff`, …), unit-tested in isolation.

### Changed

- Worktree-mode integration is always **serialized** — unit branches merge into the target one at
  a time. `max_parallel` exists as a validated knob but is clamped to `1` (inert) until internal
  parallel fan-out is built.
- Story spec paths are persisted **relative to the worktree** in `state.json`, so a kept-failed
  run's state stays portable if the worktree is later moved.
- The run reclaims its worktree scaffolding on clean completion (deliberately-kept failed/escalated
  worktrees are left in place and journalled so they can be found).
- **TUI settings editor now collapses every section by default.** Each policy section
  (`gates`, `limits`, `scm`, …) starts collapsed with a one-line description in its header, so the
  grown-large form scans at a glance — expand only the section you want to edit. `ctrl+e` toggles
  all sections open/closed at once.

### Fixed

- A detached HEAD or unborn repo no longer lands worktree merges on an unreferenced commit — the
  run pauses with a clear reason instead. A merge conflict against the target keeps the unit branch
  for manual merge and escalates; `capture_diff` now raises on a genuine `git` error (rather than
  silently truncating the patch) and `merge_branch` reports a failed abort/reset.
- **Editing settings no longer dirties the worktree for validation.** `worktree_clean()` (the
  pre-flight gate for `run`/`sweep`/`validate`) now ignores `.automator/policy.toml`, so saving a
  change in the settings editor no longer forces a commit of the config before the next command.
  Only that one file is exempt — the deferred-work ledger under `.automator/` still commits as
  before.

## [0.3.2] — 2026-06-15

### Added

- **Arrow-key navigation and Enter-to-edit on the settings screen.** Up/Down now move focus
  between fields (additive — Tab/Shift+Tab still work), and Enter activates the focused field
  by type: it opens a dropdown (`Select`), toggles a switch, or enters cursor-edit mode on the
  multi-line box (`TextArea`), where the box's own Up/Down then move the cursor; Escape leaves
  edit mode without leaving the screen. Plain text/number inputs stay editable on focus, so
  Enter is a no-op there. Implemented with priority bindings gated by `check_action` so an open
  dropdown or an editing TextArea keeps Up/Down, and Escape still pops the screen in nav mode.

### Fixed

- **Attaching to answer a deferred-work decision now returns you where you came from.**
  When a prompting sweep blocks on a decision (or you open a resolve session), pressing
  `a`/`R` switches a tmux client into the orchestrator's control window so you can answer
  there — but on exit it left you stranded in the control session on the parked
  exit-status prompt instead of back at the TUI. The control window now records where the
  attach came from and, once you press enter, returns you: it switches the client back to
  the TUI's own pane when the TUI runs inside tmux (i.e. your original session), or
  detaches the throwaway attach client so the suspended TUI resumes when it runs outside
  tmux. Windows nobody attached to interactively still park unchanged.

- **Empty optional numeric fields no longer flash a red "invalid" outline.** The start-run
  and start-sweep modals draw their numeric inputs (`epic`, `max stories`, `max bundles`)
  with `type="integer"`, which under Textual validates on blur and — with the default
  `valid_empty=False` — treats an empty string as invalid. Tabbing past a blank field that
  is explicitly optional ("blank for all", "blank for no limit") therefore tripped the red
  `$error` border. The inputs now pass `valid_empty=True`, matching the settings screen, so
  leaving them blank is accepted silently while a typed integer still validates.

### Changed

- **Clearer review toggle on the settings screen.** The `[review]` switch showed only the
  raw key `enabled`, with no hint about what it controls. It is now relabelled "separate
  review session" and carries a muted caption spelling out both states (ON: triple review
  runs in a dedicated 2nd session · OFF: quick-dev runs its own tri-review inline). The
  change is display-only — the config key and save logic are unchanged.

- **`bmad-auto-setup` now upgrades, not just installs.** Re-running the skill (or invoking
  it with `upgrade`) on an already-installed project is detected as an upgrade — it runs
  `uv tool upgrade bmad-automator --reinstall` (the `--reinstall` is required for a git
  source) and re-lays the per-project skills with `bmad-auto init --force-skills`, then
  reports the before → after version. Previously a re-run was treated as a config-only
  update: it left `--force-skills` off, so `init` silently skipped every existing skill
  dir and the project kept stale skills against the upgraded tool. Upgrade is detected from
  an existing `bauto` config section and/or a uv-managed `bmad-automator`, and the tool
  follows `main` by default with an offer to pin a release tag. Docs (README "Upgrading",
  `docs/setup-guide.md`) now describe the skill-driven upgrade alongside the manual ritual,
  and the stale `uv tool upgrade bmad-automator` hint (missing `--reinstall`) is corrected.

## [0.3.1] — 2026-06-14

Maintenance release. Also backfills the previously-undocumented `[0.3.0]` notes below.

### Changed

- `scripts/sync_version.py` now runs `uv lock` as part of the version stamp, so a
  version bump regenerates the pinned lock in one command. CI runs `uv sync --locked`,
  which fails the install step on a stale lock (hit while cutting 0.3.0); folding the
  relock into the stamp keeps a bump a single command. Idempotent, with a loud non-zero
  exit if `uv` is missing or the lock fails.

## [0.3.0] — 2026-06-14

First release carrying the optional review toggle.

### Added

- **Optional review pass** — new policy `[review] enabled` toggle (default `true`). When
  disabled, a run skips the separate fresh-context `bmad-auto-review` session: the dev pass
  runs quick-dev's own internal triple-review unattended and finalizes the story straight to
  `done` — one session per story instead of two, with verify commands still gating the
  commit. The flag flows to the dev session via `BMAD_AUTO_SKIP_REVIEW=1`; the dev skill (not
  the engine) writes the `done` status, preserving the engine-never-writes-status invariant.
  Global scope: also governs deferred-work sweep bundles. Exposed as a switch in the TUI
  settings screen.

### Changed

- **Install / upgrade docs** — the README install block now offers main-tracking vs.
  pinned-tag installs, and a new "Upgrading" section documents the two-step ritual
  (`uv tool upgrade --reinstall` — required for a git source — then re-lay per-project skills
  with `init --force-skills`). The `bmad-auto-setup` skill is corrected to use `--reinstall`
  (plain `uv tool upgrade` reuses the cached git commit and won't pull new code) and notes the
  skill re-lay step plus tag pinning.
- Regenerated `uv.lock` for the 0.3.0 version pin.

## [0.2.0] — 2026-06-14

First versioned release since the initial `0.1.0`. Consolidates everything built since then and
realigns the version across the Python package and the BMAD-module metadata (which had drifted to a
placeholder `1.0.0`). All version-bearing fields are now kept in sync by `scripts/sync_version.py`,
enforced in CI.

### Added

- **TUI dashboard** (`bmad-auto tui`) — live, read-only view of runs, the sprint tree, the
  deferred-work ledger, a per-story phase/token table, and tailing of the journal / pane log /
  ATTENTION file, plus an integrated launcher for new runs and an in-app policy editor.
- **Deferred-work sweeps** — `bmad-auto sweep` triages the ledger against the real codebase and
  runs full dev → review → verify → commit on actionable bundles; `--repeat` re-triages each cycle;
  `bmad-auto decisions` surfaces and pre-answers human decisions earlier sweeps left open.
- **Interactive escalation resolution** — `bmad-auto resolve <run-id>` opens a resolve agent to
  disambiguate a frozen spec on a CRITICAL escalation, then re-arms the story and resumes.
- **Multi-CLI / multi-agent support** — a generic tmux adapter driven by declarative TOML profiles,
  with built-in `claude` (default), `codex`, and `gemini` profiles and per-stage overrides
  (`[adapter.dev|review|triage]`) for client/model/extra args.
- **Run operations** — `stop`, `delete`, `archive`, and `cleanup` for tmux artifacts of finished or
  stopped runs.
- **Cost-weighted token budgeting** — per-story `max_tokens_per_story` using cache-read weighting.
- **Bundled skill module** — the `bmad-auto-*` skills ship inside the wheel and are laid down by
  `bmad-auto init` into `.claude/skills/` and/or `.agents/skills/`.

### Changed

- **BREAKING:** policy `[adapter]` no longer accepts the flat `model_dev` / `model_review` keys; use
  the `[adapter.dev]` / `[adapter.review]` / `[adapter.triage]` sections instead (a clear error
  points at the replacement).
- **BREAKING:** build system migrated from setuptools + pip to **hatchling + uv**. Install with
  `uv tool install "bmad-automator[tui] @ git+…"`; develop with `uv sync --all-extras`. All docs,
  CLI hints, the `bmad-auto-setup` skill, and the eval-runner Dockerfile now use uv.
- **BREAKING:** module layout renamed `module/` → `skills/`; the canonical skills live under
  `src/automator/data/skills/`.

### Fixed

- BMAD-method installer could not locate `module.yaml` for the `bauto` module
  (`collectAgentsFromModuleYaml` / `writeCentralConfig` warnings): restored a repo-root
  `module.yaml` descriptor so the installer's shallow lookup resolves the module again.
- Replaced stale `pip install` instructions across docs, CLI hints, the setup skill, the
  eval-runner Dockerfile, and the module greeting with their uv equivalents.

## [0.1.0]

- Initial release: deterministic dev → review → verify → commit orchestrator for the BMAD
  implementation phase, driven by a Python control loop with hook-based session transport and
  resumable on-disk run state.

[0.5.0]: https://github.com/pbean/bmad-automator/releases/tag/v0.5.0
[0.4.4]: https://github.com/pbean/bmad-automator/releases/tag/v0.4.4
[0.4.3]: https://github.com/pbean/bmad-automator/releases/tag/v0.4.3
[0.4.2]: https://github.com/pbean/bmad-automator/releases/tag/v0.4.2
[0.4.1]: https://github.com/pbean/bmad-automator/releases/tag/v0.4.1
[0.4.0]: https://github.com/pbean/bmad-automator/releases/tag/v0.4.0
[0.3.2]: https://github.com/pbean/bmad-automator/releases/tag/v0.3.2
[0.3.1]: https://github.com/pbean/bmad-automator/releases/tag/v0.3.1
[0.3.0]: https://github.com/pbean/bmad-automator/releases/tag/v0.3.0
[0.2.0]: https://github.com/pbean/bmad-automator/releases/tag/v0.2.0
[0.1.0]: https://github.com/pbean/bmad-automator/releases/tag/v0.1.0
