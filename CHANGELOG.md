# Changelog

All notable changes to `bmad-automator` are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the project is pre-1.0,
breaking changes may land in a minor release.

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

[0.3.1]: https://github.com/pbean/bmad-automator/releases/tag/v0.3.1
[0.3.0]: https://github.com/pbean/bmad-automator/releases/tag/v0.3.0
[0.2.0]: https://github.com/pbean/bmad-automator/releases/tag/v0.2.0
[0.1.0]: https://github.com/pbean/bmad-automator/releases/tag/v0.1.0
