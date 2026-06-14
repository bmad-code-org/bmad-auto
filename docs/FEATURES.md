# bmad-auto — Features & Functionality

For BMAD users who have run `bmad-sprint-planning` and have a `sprint-status.yaml` full of `ready-for-dev` stories. This is what the tool actually does and the problem each capability addresses.

See [README.md](../README.md) for the narrative overview and [setup-guide.md](setup-guide.md) for installation.

---

## Capability matrix (feature → problem addressed)

| Capability                           | What it does                                                                                                                            | Problem it addresses                                                                      |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Deterministic control loop           | Story selection, retries, gates, completion checks run in plain Python                                                                  | LLM-as-orchestrator is nondeterministic, hard to debug, and costs tokens for control flow |
| Trust-nothing verification           | Checks on-disk artifacts (spec status, baseline-commit match, non-empty diff, sprint sync) + runs your test/lint commands before commit | Agents claim success without working code; broken builds slip through                     |
| Fresh-context adversarial review     | Dev and review are separate sessions; review uses 3 parallel layers                                                                     | Self-review anchoring bias; implementer marks own work correct                            |
| Hook-based transport                 | Coding-agent hooks write structured event files; skills write `result.json`                                                             | Brittle terminal pane-scraping                                                            |
| Resumable state machine              | Every run is on-disk state, resumable after gate/escalation/crash                                                                       | Long unattended runs lost to interruptions                                                |
| Plateau-defer                        | Stuck stories are skipped, stashed, and the run continues                                                                               | One unconvergeable story blocking a whole sprint                                          |
| Typed escalations + resolve workflow | CRITICAL pauses + notifies; interactive resolve agent re-arms the story                                                                 | Ambiguous specs silently producing wrong code                                             |
| Deferred-work sweeps                 | Triages an append-only ledger against real code, bundles + executes                                                                     | Split-off goals and review findings get lost                                              |
| Multi-CLI adapter + profiles         | Generic tmux driver runs claude/codex/gemini; per-stage overrides; TOML profiles                                                        | Vendor lock-in; no way to mix models per stage                                            |
| Cost-weighted token budgets          | Per-story budget counts cache reads at ~0.1x; raw totals displayed                                                                      | Naive token caps misjudge real cost (cache reads dominate)                                |
| Non-invasive skill forks             | Drives its own `bmad-auto-*` skill forks; reads `sprint-status.yaml` only                                                               | Modifying a user's standard BMAD install                                                  |
| Read-only TUI + launcher             | Live dashboard over run-dir artifacts; launches detached runs                                                                           | No visibility into what an unattended run is doing                                        |

---

## Full feature list

### Core orchestration loop

- Automated per-story pipeline: `dev → verify → review → verify → commit`, end-to-end, no human in the loop.
- Deterministic control flow in plain Python — story selection, retry budgets, gate checks, and completion checks are code, not an LLM session.
- Reads `sprint-status.yaml` as the single source of truth (owned by BMAD skills; orchestrator only reads it); selects the next `ready-for-dev` story; advances by epic/story.
- Scoping flags: `--epic N`, `--story KEY`, `--max-stories N`, `--dry-run` (prints the plan, spawns nothing).

### Spec + implementation (dev stage)

- Drives `bmad-auto-dev` (fork of `bmad-quick-dev`) in a fresh tmux session: plans a 1.5–4k-token spec, auto-approves it, implements, syncs `sprint-status` to `in-review`, writes `result.json`.
- Spec-only contract between stages — review consumes the frozen spec, not the dev session's context.

### Verification (trust-nothing gate)

- After each session, checks on-disk artifacts before proceeding: spec frontmatter status, independent baseline-commit match (an LLM-lie detector), non-empty diff, sprint-status sync.
- Runs _your_ commands (`[verify].commands`, e.g. `pytest -q`, `ruff check .`) — a broken build never reaches review or commit.

### Adversarial review (review stage)

- Drives `bmad-auto-review` (fork of `bmad-code-review`) in a separate, fresh-context session — no anchoring bias from the implementer.
- Static prefilter → 3 parallel layers (Blind Hunter / Edge Case Hunter / Acceptance Auditor) → verify findings against code → triage → auto-apply patches → log → defer ambiguity.
- Bounded review loop (default 3 cycles); done when clean.

### Failure handling & resilience

- Bounded dev retries (default 2): verify-failures keep the tree and feed the failing output to the next session via `--feedback`; other failures roll back to baseline.
- Plateau-defer: when review won't converge, the story is skipped, the spec stashed into the run dir, deferred-work preserved, the run continues.
- Typed escalations: `CRITICAL` pauses the run + notifies (desktop + `ATTENTION` file); `PREFERENCE` is journaled and continues.
- CRITICAL resolution: `bmad-auto resolve <run-id>` opens an interactive resolve agent seeded with the escalation + frozen spec; you disambiguate, it re-arms the story (`escalated → pending`, spec reset to `ready-for-dev`) and resumes. `--no-interactive` skips to re-arm if you fixed the spec yourself.

### Resumability & state

- Every run is a resumable on-disk state machine: `bmad-auto resume <run-id>` continues from a gate, escalation, or interruption.
- All run state in `.automator/runs/<run-id>/` (gitignored): `state.json`, `journal.jsonl` (every decision), `events/` (hook signals), `tasks/<id>/`, `logs/`, `deferred/`, `resolve/`, `ATTENTION`.

### Hook-based transport (no pane-scraping)

- Coding-agent hooks (`Stop` / `SessionStart` / `SessionEnd` / `PreCompact`) write structured event files the orchestrator watches; skills write a machine-readable `result.json`.

### Deferred-work sweeps

- Skills accumulate an append-only ledger (`deferred-work.md`, `DW-<n>` entries): split-off goals, pre-existing findings, "needs human decision" items.
- `bmad-auto sweep` triages every open entry against the actual code (ledger statuses treated as unreliable) → partition: already-resolved (auto-closed with evidence) / bundles / blocked / skip / decisions.
- Bundles run the full pipeline (dev `--dw-bundle` → review → verify → commit); the review gate checks every bundle entry is `status: done`.
- Interactive decision walkthrough (build / close / keep-open per option, with a recommendation); answers written back as `decision:` lines. Unattended runs leave decisions open.
- Auto-sweep at epic boundaries or run-end (`[sweep] auto`); a failed/paused child sweep never interrupts the parent run.
- Repeat mode (`--repeat` / `[sweep] repeat`): re-triages after each cycle to absorb newly generated deferred work, stopping when a cycle does nothing addressable or hits `max_cycles`.
- Sweeps are their own resumable runs (`bmad-auto resume <id>`).

### Gates & human checkpoints

- Gate modes (`[gates].mode`): `none` (fully unattended) / `per-epic` (pause at epic boundaries, default) / `per-story-spec-approval` (pause after each spec for approval).
- Retrospective handling (`retrospective = never | notify | auto`) and notification on epic boundaries.

### Multi-CLI / multi-agent support

- Generic tmux adapter drives any CLI fitting the tmux-injection + hook-signal transport; CLI specifics live in declarative TOML profiles.
- Supported, E2E-verified: `claude` (reference), `codex` (≥ 0.139), `gemini` (≥ 0.46).
- Per-stage CLI/model overrides: run dev on one CLI/model, review on another (`[adapter.dev]`, `[adapter.review]`, `[adapter.triage]`).
- Add a CLI without touching Python: drop a TOML profile in `.automator/profiles/<name>.toml` (binary, prompt template, bypass flags, hook dialect, native→canonical event map).

### Budgeting & cost tracking

- Per-story token budget (`max_tokens_per_story`) using a cost-weighted total — cache reads counted at `cache_read_weight` (default 0.1, matching ~0.1x vendor billing); displayed totals stay raw.
- Token usage read from each CLI's local session transcript (per-profile `usage_parser`), aggregated per story (`bmad-auto status`).

### Configuration (`.automator/policy.toml`)

- Single policy file written by `init`, snapshotted at run start (applies to new runs and resumes; editable live from the TUI).
- Sections: `[gates]`, `[limits]`, `[verify]`, `[notify]`, `[adapter]` (+ per-stage), `[sweep]`.
- Tunable limits: `max_review_cycles`, `max_dev_attempts`, `session_timeout_min`, `stop_without_result_nudges`, `max_tokens_per_story`.

### TUI dashboard

- Read-only observer + launcher (`bmad-auto tui`): runs table, expandable sprint tree (epics → stories/retro), severity-colored deferred-work ledger, per-story phase table (phase · dev attempts · review cycles · tokens · commit/defer), tabs tailing journal / pane log / `ATTENTION`.
- Launch & manage from keys: start run/sweep (`r`/`s`), resume (`e`), resolve escalation (`R`), attach (`a`), cleanup (`c`), validate (`v`), settings editor (`g`), dark mode / quit (`d`/`q`).
- Survives TUI exit/crash: runs launched from the TUI are detached `bmad-auto` processes in a dedicated `bmad-auto-ctl` tmux session; the dashboard watches purely via run-dir artifacts, so shell-started runs appear identically.
- Comment-preserving policy editor (`g`): grouped form, validated with the engine's own parser, unset keys show defaults as placeholders.

### tmux session management

- Each run drives agents in a dedicated `bmad-auto-<run-id>` session; `attach` to watch live.
- Auto-teardown on finish (`cleanup_session_on_finish`, disable to inspect); `stop` always kills it; paused/interrupted runs keep the session for `resume`.
- `bmad-auto cleanup` (or `c` in the TUI) sweeps leftover sessions/windows for finished/stopped/orphaned runs; live runs are never touched.

### Setup & install

- `bmad-auto init` installs the four `bmad-auto-*` skills (`.claude/skills/` and/or `.agents/skills/`), the hook relay, `.automator/policy.toml`, and a runs-dir gitignore. Flags: `--cli` (repeatable), `--no-skills`, `--force-skills`.
- `bmad-auto validate` preflights every prerequisite: BMAD config, sprint-status, git, tmux, CLI binary, hook registration.
- Non-invasive: drives its own forks of the dev/review skills — your standard BMAD install is never modified. Upstream improvements are merged by diffing fork vs. upstream (forks keep the upstream file structure).

### Command reference

- `bmad-auto init` — install skills, hooks, policy, gitignore.
- `bmad-auto validate` — preflight all prerequisites.
- `bmad-auto run` — drive the dev → review → verify → commit loop.
- `bmad-auto sweep` — triage + execute open deferred-work entries.
- `bmad-auto resume <run-id>` — continue a paused/interrupted run.
- `bmad-auto resolve <run-id>` — resolve a CRITICAL escalation, then re-arm + resume.
- `bmad-auto status [<run-id>]` — run + sprint summary with per-story token totals.
- `bmad-auto attach [<run-id>]` — tmux-attach to a run's live agent session.
- `bmad-auto cleanup` — remove leftover tmux artifacts for finished/stopped runs.
- `bmad-auto tui` — the interactive dashboard.
- Every command takes `--project <dir>` (default: current directory).
