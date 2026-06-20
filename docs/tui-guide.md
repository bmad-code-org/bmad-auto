# TUI guide

`bmad-auto tui` is a live terminal dashboard for everything the orchestrator
does: watching runs, launching new ones, resuming paused ones, answering sweep
decisions, and editing policy. This guide covers every screen, key, and
message. For the one-page summary, see the [TUI section of the
README](../README.md#tui).

## Installation and launch

```bash
uv sync --extra tui        # adds textual + tomlkit; the core stays pyyaml-only
cd /path/to/your/bmad/project
bmad-auto tui              # or: bmad-auto tui --project /path/to/project
```

`--project` defaults to the current directory. tmux must be on PATH for the
launch/attach keys (`r` `s` `e` `a`); pure observation works without it.

Over a slow or high-latency link (SSH, Tailscale), a 60fps update stream can't
drain in time and partial frames paint over old ones. Launch with
`bmad-auto tui --low-frame-rate` to cap Textual to 15fps and disable
animations (sets `TEXTUAL_FPS` / `TEXTUAL_ANIMATIONS`), or make it permanent
with `[tui] low_frame_rate = true` in `policy.toml` (editable from the settings
screen). An explicit `TEXTUAL_FPS` in the environment still wins.

## Architecture: observer/launcher, never the engine

The TUI never runs an engine in-process. The two halves:

- **Launcher** — `r`, `s`, and `e` spawn detached `bmad-auto` processes as
  windows of a dedicated tmux session, `bmad-auto-ctl`. Windows are named
  `run-<run-id>`, `sweep-<run-id>`, or `resume-<run-id>`, run the same Python
  interpreter as the TUI (`python -m automator.cli`, immune to PATH/venv drift
  inside tmux), and stay open after exit showing
  `[bmad-auto exited <code> — press enter]` so you can inspect failures.
  Quitting or crashing the TUI does not touch them. The engine each run drives
  lives in a separate `bmad-auto-<run-id>` session; it is torn down when the run
  finishes (unless `[adapter] cleanup_session_on_finish = false`). These parked
  `bmad-auto-ctl` windows and any leftover `bmad-auto-<id>` sessions can be
  swept with `c` (see [Cleaning up sessions](#cleaning-up-sessions-c)).
- **Observer** — the dashboard reads only the artifacts the engine writes
  atomically into `.automator/runs/<run-id>/`: `state.json`, `journal.jsonl`,
  `logs/<task-id>.log`, `ATTENTION`, `engine.pid`. It polls the selected run
  every second (run list, sprint status, and the deferred-work ledger every 3
  seconds) with stat-gated readers, so unchanged files are never re-parsed.
  Runs started from a plain shell show up identically — the TUI has no
  privileged channel.

Fast read-only commands (`validate`, dry runs) are the exception: they are
captured and shown in a scrollable modal instead of spawned in tmux.

## Dashboard layout

```text
┌─ bmad-auto — /path/to/project ─────────────────────────────────────────┐
│ st run              type │ 20260611-091500-3f2a  ▶ running             │
│ ✔  20260610-…       story│ started 2026-06-11T09:15:00  epic 2         │
│ ▶  20260611-…       story│ tasks 8  done 5  deferred 1  escalated 0    │
├──────────────────────────┤─────────────────────────────────────────────┤
│ ▼ Epic 1 · 4/4 ✓         │ story         phase           dev review …  │
│ ▼ Epic 2 · 1/3           │ 2-3-billing   review-running  ×1  ×2     …  │
│   ✓ 1-auth               ├─────────────────────────────────────────────┤
│   ▶ 2-search             │ Journal │ Log │ Attention                   │
├──────────────────────────┤ 09:15:02 session-start   task_id=…          │
│ DW-1 Fix flaky retry     │                                             │
│ DW-2 ✓ Polish help text  │                                             │
├──────────────────────────┴─────────────────────────────────────────────┤
│ q quit  r run  s sweep  e resume  a attach  v validate  g settings  …  │
└─────────────────────────────────────────────────────────────────────────┘
```

…and the same layout, live:

<div align="center">
<img src="images/dashboard.png" alt="The bmad-auto TUI dashboard, fully populated." width="880">
</div>

### Left column

Three stacked panes; `tab` / `shift+tab` move focus between them. The sprint
and deferred panes read project-level files maintained by LLM sessions
(`sprint-status.yaml`, `deferred-work.md`), so both parse forgivingly: a
missing or malformed file shows a dim placeholder instead of an error, and
the pane recovers on the next poll once the file is readable again.

#### Run list (top)

One row per run dir under `.automator/runs/`, oldest first (run ids are
`YYYYMMDD-HHMMSS-<hex>` and sort chronologically). Columns: `st` (status
glyph, see below), `run` (the id), `type` (`story` or `sweep`). On first load
the newest run is auto-selected; arrow keys or mouse select another. A run you
just launched is selected immediately, before its directory even exists.

#### Sprint tree (middle)

Sprint status from `sprint-status.yaml` as one expandable node per epic —
`Epic N · done/total`, fully green with a `✓` once every story is done.
Enter (or click) expands an epic to its stories and retrospective, each with
a status glyph:

| Glyph | Status                     | Color   |
| ----- | -------------------------- | ------- |
| `✓`   | done                       | green   |
| `▶`   | in-progress                | cyan    |
| `◆`   | review                     | magenta |
| `○`   | ready-for-dev              | cyan    |
| `·`   | backlog / optional (retro) | dim     |
| `?`   | anything unrecognized      | dim     |

Expansion state and the cursor survive the 3-second refresh — only labels are
updated in place unless an epic's story set actually changes.

#### Deferred work (bottom)

Every entry from the `deferred-work.md` ledger, in file order: `DW-<n>` plus
the title, truncated to the pane width. Done entries are green with a `✓`;
open entries are color-coded by the entry's optional `severity:` field —
critical (bold red), high (red), medium (yellow), low (dim), unspecified
(plain). Arrow keys navigate; `enter` opens the full entry body in a
scrollable modal (`escape` closes).

### Run header (top right)

A one-glance summary of the selected run: id, `[sweep]` tag for sweep runs,
status glyph + word, start timestamp, current epic, and a counts line —
`tasks N · done (green) · deferred (yellow) · escalated (red when nonzero) ·
total tokens`. Below that, situational banners:

- `⏸ paused (<stage>) — <reason> · press e to resume` — gate or escalation
  pause; stages are `spec-approval`, `epic-boundary`, `escalation`,
  `story-gate`. At the `escalation` stage, `e` only skips the escalated story —
  press `R` instead to resolve it (see "Resolving an escalation" below).
- `✖ engine gone — run was interrupted · press e to resume` — the recorded
  engine pid is dead.
- `⚑ decision needed: DW-<n> — <question> / press a to attach and answer` —
  an attended sweep is blocked on a human decision (see below).
- `⧗ starting… waiting for the engine to write state.json` — just launched;
  if nothing appears within 10 seconds the TUI raises a "launch may have
  failed" error toast.

### Task table (middle right)

One row per story (or sweep bundle/triage task) in the selected run:

| Column   | Meaning                                                                                                                                                                                                      |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `story`  | story key, or the sweep task id                                                                                                                                                                              |
| `phase`  | `pending` → `dev-running` → `dev-verify` → `review-running` → `review-verify` → `committing` → `done`; terminal alternatives `deferred` / `escalated`; sweep triage shows `triage-running` / `triage-verify` |
| `dev`    | dev attempt counter, `×N`                                                                                                                                                                                    |
| `review` | review cycle counter, `×N`                                                                                                                                                                                   |
| `tokens` | raw token total for the story, `-` until known                                                                                                                                                               |
| `info`   | defer reason, or the commit SHA (first 12 chars) once committed                                                                                                                                              |

### Tabs (bottom right)

- **Journal** — every engine decision, live-tailed from `journal.jsonl`. Line
  format: `HH:MM:SS  <kind>  field=value …` (long values truncated with `…`).
  Kinds are color-coded — see the reference below.
- **Log** — the active agent session's pane output (`logs/<task-id>.log`),
  ANSI colors preserved, starting with a dim `— <task-id>.log —` header. The
  active task is the last `session-start` without a matching `session-end`
  (falling back to the newest log file); the tab switches automatically when
  the engine moves to the next session. Only the last 64 KB of a large log is
  read on first open.
- **Attention** — the run's `ATTENTION` file (escalations, gate
  notifications). New lines after the first poll also fire a warning toast.

## Status reference

Run status is classified from `state.json` plus a liveness probe:

| Glyph | Status      | Color    | Meaning                                                     |
| ----- | ----------- | -------- | ----------------------------------------------------------- |
| `▶`   | running     | green    | not finished, not paused, engine pid alive                  |
| `⏸`   | paused      | yellow   | engine is waiting at a gate or escalation — `e` resumes     |
| `✔`   | finished    | dim      | run completed                                               |
| `✖`   | interrupted | bold red | engine pid is dead but the run never finished — `e` resumes |
| `?`   | unknown     | dim      | liveness can't be determined, or `state.json` is unreadable |

Liveness is **local-only**: `engine.pid` is checked with `os.kill(pid, 0)`.
A run driven on another host (shared checkout) always shows `unknown`, never
falsely `interrupted`. Legacy runs without a pid file fall back to probing the
per-run tmux session, which can prove `alive` but never `dead`.

Journal kinds are styled by substring, first match wins:

| Substring                                       | Color  | Examples                                        |
| ----------------------------------------------- | ------ | ----------------------------------------------- |
| `escalat`, `failed`                             | red    | `preference-escalation`, `review-verify-failed` |
| `done`, `complete`, `finished`                  | green  | `story-done`, `run-complete`                    |
| `decision`, `deferred`, `boundary`, `truncated` | yellow | `decision-pending`, `epic-boundary`             |
| `start`, `resume`                               | cyan   | `session-start`, `run-resume`                   |
| anything else                                   | dim    |                                                 |

## Key bindings

| Key | Action                                                                     |
| --- | -------------------------------------------------------------------------- |
| `r` | start a run (modal)                                                        |
| `s` | start a sweep (modal)                                                      |
| `e` | resume the selected paused/interrupted run (confirm modal)                 |
| `R` | resolve a run paused at an escalation (interactive, then re-arm)           |
| `d` | answer deferred-work decisions past sweeps left unanswered (modal walk)    |
| `a` | attach to the selected run's live session or orchestrator window           |
| `x` | stop the selected live run (confirm modal)                                 |
| `D` | delete the selected run's directory (confirm modal)                        |
| `A` | archive the selected run to `.automator/archive` (confirm modal)           |
| `c` | clean up tmux sessions/windows for finished & stopped runs (confirm modal) |
| `v` | run `bmad-auto validate`, output in a modal                                |
| `g` | settings editor for `.automator/policy.toml`                               |
| `M` | toggle theme (light/dark mode)                                             |
| `q` | quit (running engines are unaffected)                                      |

In the settings editor: `ctrl+s` saves, `ctrl+e` expands/collapses all
sections, `escape` goes back without saving. In any modal: `escape` cancels.

## Starting runs and sweeps (`r` / `s`)

`r` opens the **start run** modal — all fields optional:

- **epic** — integer, restrict to one epic; blank = all
- **story key** — restrict to one story; blank = all
- **max stories** — stop after N stories; blank = no limit
- **dry run** — print the plan, spawn nothing (output shown in a modal)

`s` opens the **start sweep** modal:

- **unattended (`--no-prompt`)** — skip decision prompts, leave decisions open
- **decisions only** — triage + answer decisions, run no bundles
- **max bundles** — override the policy's `[sweep] max_bundles`; blank = policy default
- **dry run** — list open ledger entries, spawn nothing

Before any real launch the TUI applies the same guard as the CLI:

1. tmux must be on PATH.
2. The git worktree must be clean — otherwise an error toast, no launch.
3. If another run on this project is currently `running`, a confirmation
   modal lists it and asks before you "launch anyway" (two engines on one
   project may conflict).

On success a toast names the run id and the `bmad-auto-ctl` session, and the
dashboard selects the new run, showing `⧗ starting…` until `state.json`
appears.

## Resuming (`e`)

`e` acts on the selected run. It refuses runs that are already finished or
whose state is unreadable. The confirmation modal shows what you are resuming:

- paused runs: `paused at <stage> — <reason>` in yellow;
- non-paused runs: `run is not paused — it looks interrupted` (dim);
- and, in bold red, `engine.pid is still alive — resuming would double-drive
this run` when the original engine still appears to be running. Heed this
  one: two engines driving one run dir corrupt each other's state. It can also
  mean the pid was recycled by another process — verify before resuming.

Confirming spawns `bmad-auto resume <run-id>` detached in `bmad-auto-ctl`,
like any other launch. Resume drops any stale `bmad-auto-<run-id>` session a
stopped or interrupted run left behind and spins up a fresh one, so the run
never re-attaches to a dead session.

## Cleaning up sessions (`c`)

`c` removes leftover tmux artifacts for the current project in one pass, after a
confirmation modal: every `bmad-auto-<run-id>` agent session whose run has
finished, stopped, or crashed (and any orphan whose run dir is gone), plus the
parked `[bmad-auto exited …]` windows in `bmad-auto-ctl`. Live runs, the window
you triggered the cleanup from, and any session or window belonging to another
project are always spared, so it is safe to press at any time even with other
projects' runs in flight. A toast reports how many sessions and windows were closed. The same
sweep is available from a plain shell as `bmad-auto cleanup` (`--dry-run` to
preview). Runs already tear their own session down on finish unless you set
`[adapter] cleanup_session_on_finish = false`; `c` is for the backlog that
predates that, or that the flag deliberately keeps around.

## Resolving an escalation (`R`)

`R` is the escalation-specific counterpart to `e`. It is only offered for a run
paused at the `escalation` stage (otherwise it warns and does nothing) and
refuses a run whose engine is still live. A CRITICAL escalation parks its story
in a terminal `escalated` phase that plain `resume` skips — `R` is how you get
it un-stuck.

Confirming launches `bmad-auto resolve <run-id>` in a `bmad-auto-ctl` window and
**attaches you to it** (the resolve agent is interactive). You converse with the
agent — it is seeded with the escalation detail and the frozen spec — to
disambiguate the spec. When it has recorded a resolution, the same window prompts
`re-arm <story> and resume run <id>? [y/N]`; answer `y` and it re-arms the story
(`escalated → pending`, spec status reset to `ready-for-dev`) and resumes the run
in place — a clean rebuild against the corrected spec, then on through the rest
of the sprint. Detach (`Ctrl-b d`) to return to the dashboard, which observes the
resumed run like any other. Exiting the agent without recording a resolution
leaves the story escalated and the run paused — the safe default.

## Attaching (`a`) and the sweep decision flow

`a` picks its target in this order:

1. **Decision-blocked sweep, or no live agent session** → the run's
   orchestrator window in `bmad-auto-ctl` (only exists for runs launched from
   the TUI).
2. **Live agent session** → the per-run tmux session `bmad-auto-<run-id>`
   where the coding CLI is working.
3. Neither → a warning; there is nothing to attach to (runs started outside
   the TUI between sessions, finished runs).

If the TUI itself is running inside tmux, attach uses `switch-client` — the
TUI keeps running and you switch back with your usual tmux client commands.
Outside tmux, the TUI suspends, runs `tmux attach`, and resumes when you
detach (`ctrl-b d`).

### Answering a sweep decision

An attended sweep that reaches a "needs human decision" entry blocks on its
own terminal prompt. The dashboard spots the `decision-pending` journal event
and shows the `⚑ decision needed: DW-<n>` banner plus a one-time warning
toast. Then:

1. Press `a` — with a decision pending this always targets the sweep's
   orchestrator window, where the prompt is waiting.
2. Answer the prompt (build / close / keep-open, with the triage
   recommendation shown).
3. Detach with `ctrl-b d`.

The banner clears on the next poll after the sweep journals anything further
(the answer is recorded as a `decision:` line in `deferred-work.md`). Sweeps
launched with **unattended** never prompt, so this flow only applies to
attended sweeps.

### Answering missed decisions (`d`)

The flow above is for a decision a _live_ attended sweep is blocked on. For
decisions an **unattended** sweep skipped — or an attended one you walked away
from — press `d`. The Deferred Work pane title shows the outstanding count
(`Deferred Work — N to answer (d)`), and `d` walks them one modal at a time
(question, context, and each option with its effect and the triage
recommendation). Each answer is durable: a `close` is applied immediately, and
a `build`/`keep-open` is saved to `.automator/decisions.json`, so the next sweep
acts on it (build → bundle, keep-open → recorded) without asking again. Skip a
modal to leave that one for later. The same set is available on the CLI via
`bmad-auto decisions` (`--list` to just view).

## Validate (`v`)

Runs `bmad-auto validate --project <project>` in the background and shows the
combined output in a scrollable modal titled `validate — ok` (or
`exit <code>`). Same preflight as the CLI: config, sprint-status, git, tmux,
CLI binary, hooks.

## Settings editor (`g`)

Edits `.automator/policy.toml` **comment-preservingly** (tomlkit): saving only
rewrites keys you actually changed; everything else — comments, order,
formatting — stays byte-identical. A missing policy file starts from the full
inline-documented template. The note at the top is load-bearing: **running
engines snapshot policy at start — changes apply to new runs and resumes.**

The form is grouped by TOML section. Every section starts **collapsed** with a
one-line description in its header, so the grown-large form scans at a glance —
expand only the section you want to edit, or press `ctrl+e` to toggle them all
open/closed at once. Unset keys show their default as a placeholder rather than
a baked-in value; clearing a field deletes the key, restoring default/inherit
behavior.

| Section.key                           | Type                   | Default            | Notes                                                                                                                                                |
| ------------------------------------- | ---------------------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gates.mode`                          | select                 | `per-epic`         | `none` / `per-epic` / `per-story-spec-approval`                                                                                                      |
| `gates.retrospective`                 | select                 | `notify`           | `never` / `notify` / `auto`                                                                                                                          |
| `limits.max_review_cycles`            | int ≥ 1                | 3                  | review loop bound before plateau-defer                                                                                                               |
| `limits.max_dev_attempts`             | int ≥ 1                | 2                  | dev retry budget                                                                                                                                     |
| `limits.session_timeout_min`          | int ≥ 1                | 90                 | per-session wall clock                                                                                                                               |
| `limits.stop_without_result_nudges`   | int ≥ 0                | 1                  | nudges when a session stops without result.json                                                                                                      |
| `limits.max_tokens_per_story`         | int ≥ 1                | 2000000            | cost-weighted budget                                                                                                                                 |
| `limits.cache_read_weight`            | float 0.0–1.0          | 0.1                | cache-read weight in the budget; 1.0 = raw                                                                                                           |
| `verify.commands`                     | one per line           | (none)             | test/lint commands run before commit                                                                                                                 |
| `notify.desktop`                      | switch                 | on                 | desktop notifications                                                                                                                                |
| `notify.file`                         | switch                 | on                 | ATTENTION file logging                                                                                                                               |
| `adapter.name`                        | text                   | `claude`           | CLI profile: `claude` / `codex` / `gemini` / custom                                                                                                  |
| `adapter.model`                       | text                   | (CLI default)      | model override                                                                                                                                       |
| `adapter.extra_args`                  | override switch + args | profile defaults   | see below                                                                                                                                            |
| `adapter.cleanup_session_on_finish`   | switch                 | on                 | kill the run's tmux session on finish; off keeps it                                                                                                  |
| `adapter.dev` / `.review` / `.triage` | text ×2 + args         | inherit            | per-stage `name` / `model` / `extra_args` overrides                                                                                                  |
| `sweep.auto`                          | select                 | `never`            | `never` / `per-epic` / `run-end`                                                                                                                     |
| `sweep.max_bundles`                   | int ≥ 1                | 5                  | bundles per sweep; triage excess truncated                                                                                                           |
| `sweep.max_triage_attempts`           | int ≥ 1                | 2                  | triage validation retries                                                                                                                            |
| `sweep.repeat`                        | switch                 | off                | re-triage after each cycle, continue on new work                                                                                                     |
| `sweep.max_cycles`                    | int ≥ 1                | 5                  | cycle cap per sweep run when repeat is on                                                                                                            |
| `scm.isolation`                       | select                 | `none`             | `none` (work in place) / `worktree` (per-unit worktree + merge-back)                                                                                 |
| `scm.branch_per`                      | select                 | `story`            | worktree mode: branch per `story`, or one shared branch per `run` (forces delete-branch off)                                                         |
| `scm.target_branch`                   | text                   | (run-start branch) | worktree mode: branch units merge back into (created if missing)                                                                                     |
| `scm.merge_strategy`                  | select                 | `merge`            | worktree mode: `ff` / `merge` / `squash`                                                                                                             |
| `scm.delete_branch`                   | switch                 | on                 | worktree mode: delete the unit branch after a successful merge                                                                                       |
| `scm.keep_failed`                     | switch                 | on                 | keep a failed unit's worktree + branch mounted for inspection                                                                                        |
| `scm.seed_adapter_defaults`           | switch                 | on                 | worktree mode: seed each loaded adapter's gitignored MCP/CLI configs (`.mcp.json`, `.claude/settings.json`, `.codex/config.toml`…) into the worktree |
| `scm.worktree_seed`                   | one per line           | (none)             | worktree mode: extra project-relative gitignored files to seed, on top of the adapter defaults                                                       |
| `scm.commit_message_template`         | text                   | (built-in)         | story/bundle commit message; `{story_key}` / `{run_id}` substituted                                                                                  |
| `scm.failed_diff_max_mb`              | int ≥ 1                | 5                  | per-file cap (MB) for untracked files in a kept-failed unit's `changes.patch`                                                                        |
| `scm.failed_diff_unlimited`           | switch                 | off                | lift the failed-diff size cap (warns when active)                                                                                                    |
| `plugins.unity.editor_mode`           | select                 | `shared`           | `shared` (needs `scm.isolation = none`) / `per_worktree` (needs `isolation = worktree`)                                                              |
| `plugins.unity.mcp`                   | select                 | `ivanmurzak`       | Editor MCP the plugin targets: `ivanmurzak` / `coplaydev`                                                                                            |
| `plugins.unity.unity_path`            | text                   | (auto-detect)      | explicit Editor binary for a `per_worktree` launch; ignored in shared mode                                                                           |
| `plugins.unity.ready_timeout_sec`     | int ≥ 1                | 600                | readiness-gate budget for the Editor + MCP to come up                                                                                                |
| `plugins.unity.ready_grace_sec`       | int ≥ -1               | -1                 | pre-probe delay; `-1` = auto (120s cold `per_worktree`, 0s warm `shared`)                                                                            |
| `tui.low_frame_rate`                  | switch                 | off                | cap to 15fps + disable animations (slow/SSH links); applies next launch                                                                              |

(`scm.max_parallel` is intentionally **not** exposed in the editor — it stays
inert, clamped to 1, until parallel fan-out is built.)

The form is **registry-driven**: the core sections above are described by
`automator/data/settings/core.toml` (presentation only — defaults and options are
referenced from the policy dataclasses, never duplicated), and every **enabled** plugin's
`[[settings]]` are appended automatically under a collapsible section of their own,
persisting to `[plugins.<name>]`. So the `plugins.unity.*` rows above only appear once
`unity` is listed in `[plugins] enabled` — the opt-in layer for game projects that drive a
live Editor through an MCP, off by default. A custom plugin dropped at
`.automator/plugins/<name>/` surfaces the same way. See [Writing a bmad-auto
plugin](plugin-authoring-guide.md) for the settings schema, [Writing a Game Engine
plugin](game-engine-plugin-guide.md), and [Writing a plugin for a specific Editor
MCP](game-engine-mcp-guide.md). The Unity plugin's `editor_mode` ↔ `scm.isolation`
coupling is validated on save, so an invalid combo (e.g. `per_worktree` with
`isolation = none`) blocks with an error.

`extra_args` fields are special: the switch distinguishes "use the profile's
default flags" (off — the key stays absent) from "replace them with exactly
this list" (on — the input is parsed shell-style; an empty list is a valid
override and is not the same as unset).

`ctrl+s` validates the whole document through the engine's own parser
(`policy.loads()`) before writing; errors land in a red strip above the
buttons and block the save. The write itself is atomic (temp file +
`os.replace`).

## Troubleshooting

| Message                                                                             | Cause / fix                                                                                                                          |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `tmux not found on PATH — launch/attach disabled`                                   | install tmux; the dashboard still works read-only                                                                                    |
| `git worktree is not clean — commit or stash first`                                 | the launch guard; commit/stash and retry. `.automator/policy.toml` is exempt, so saving in the settings editor never blocks a launch |
| `another run is live: <ids>`                                                        | a second engine on the same project may conflict — confirm only if you know they won't touch the same stories                        |
| `launch may have failed — attach to tmux session bmad-auto-ctl`                     | no `state.json` within 10 s of launch; attach to the ctl window to read the error (the window stays open with the exit code)         |
| `no run selected`                                                                   | `e` / `a` need a selected run — the project has no runs yet                                                                          |
| `state for run <id> is unreadable`                                                  | corrupt/missing `state.json`; inspect the run dir                                                                                    |
| `run <id> already finished`                                                         | finished runs can't be resumed                                                                                                       |
| `nothing to attach: no live agent session … runs started outside the TUI have none` | between sessions there is no agent window, and shell-started runs have no ctl window; wait for the next session or attach manually   |
| `cannot suspend here — run manually: tmux attach …`                                 | the terminal can't suspend the TUI; run the printed command in another terminal                                                      |
| `engine.pid is still alive — resuming would double-drive this run`                  | the original engine still runs (or its pid was recycled); attach and check before resuming                                           |
| `policy.toml is not valid TOML: …`                                                  | hand-edited file is syntactically broken; fix it in an editor — the settings screen needs a parseable document to start from         |
| sprint tree shows `sprint status unavailable`                                       | missing/invalid `_bmad/bmm/config.yaml` or sprint-status.yaml; run `bmad-auto init` / `bmad-sprint-planning`                         |
| deferred pane shows `deferred ledger unavailable`                                   | missing/unreadable `deferred-work.md`; normal until the first session defers something                                               |
| header shows `state unavailable`                                                    | the run dir exists but `state.json` is missing or never parsed; usually transient at launch                                          |

Degradation is graceful by design: a mid-write or missing file never crashes a
poll — the dashboard keeps the last good state (`?` / `unknown` where it has
none), and catches up on the next tick.
