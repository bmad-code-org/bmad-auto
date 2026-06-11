# bmad-auto

Deterministic ralph-loop orchestrator for the [BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD)
implementation phase. A plain Python program drives the loop — pick story →
implement → adversarial review → verify → commit — while LLMs do only the
creative work inside disposable, fresh-context **interactive Claude Code**
sessions running in tmux windows you can attach to and watch.

Built as a token-optimized replacement for
[bmad-automator](https://github.com/bmad-code-org/bmad-automator), whose
orchestrator is itself an LLM session interpreting prose rules and
screen-scraping tmux panes. Here:

- **No LLM in the control loop.** Story selection, retry budgets, gates, and
  completion checks are code, not prompts.
- **No pane-scraping.** Claude Code hooks (Stop / SessionStart / SessionEnd /
  PreCompact) write structured event files the orchestrator watches; skills in
  automation mode write a machine-readable `result.json` at the end of each
  workflow.
- **Trust nothing, verify everything.** After each session the orchestrator
  checks artifacts on disk: spec frontmatter status, baseline-commit match
  (recorded independently — a cheap LLM-lie detector), non-empty diff,
  sprint-status sync, and your own test/lint commands before any commit.
- **sprint-status.yaml is the single source of truth** and the orchestrator
  never writes it — only the BMAD skills do (via their idempotent
  sync-sprint-status step).
- **Fresh context per step.** Dev and review are separate sessions; review
  never shares the implementer's context (no anchoring bias).

## Requirements

- Python 3.11+, tmux, and a supported coding CLI (`claude` by default; `codex`
  and `gemini` via profiles — see "Other coding CLIs")
- A BMAD v6 project (`_bmad/bmm/config.yaml`, sprint-status.yaml from
  `bmad-sprint-planning`) with the automation-mode-enabled skills from this
  repo (`bmad-quick-dev`, `bmad-code-review`)

## Quick start

```bash
pip install -e .

cd /path/to/your/bmad/project
bmad-auto init        # installs hooks + .automator/policy.toml + gitignore
bmad-auto validate    # preflight: config, sprint-status, git, tmux, claude, hooks
bmad-auto run --dry-run   # print the plan without spawning anything
bmad-auto run             # go
bmad-auto attach          # watch the live Claude sessions in tmux
bmad-auto status          # run + sprint summary
bmad-auto resume <run-id> # continue after a gate pause or escalation
```

One-time setup: if Claude Code has never run in the target project, start it
once (`claude`) and accept the workspace-trust dialog (and any hooks-approval
prompt) before `bmad-auto run` — spawned sessions cannot answer first-run
dialogs, and a pending dialog reads as a session timeout to the orchestrator.

## How a story flows

```
sprint-status.yaml: 1-2-account-mgmt: ready-for-dev
  │
  ├─ DEV     tmux window: claude "/bmad-quick-dev 1-2-account-mgmt"
  │          quick-dev (automation mode): plans a 1.5–4k-token spec,
  │          auto-approves it, implements, syncs sprint → review,
  │          writes result.json … Stop hook signals the orchestrator
  ├─ VERIFY  spec exists · status in-review · baseline matches · diff non-empty
  ├─ REVIEW  fresh window: claude "/bmad-code-review <spec>"
  │          3 layers (Blind Hunter / Edge Case Hunter / Acceptance Auditor)
  │          → triage → auto-apply patches → defer ambiguity → done when clean
  │          (bounded loop, default 3 cycles)
  ├─ VERIFY  spec done · sprint done · run [verify].commands (pytest, ruff…)
  └─ COMMIT  orchestrator commits; epic boundary → gate / retro notification
```

Failure handling: bounded dev retries (rollback to baseline between attempts),
**plateau-defer** when review won't converge (story skipped, spec stashed into
the run dir, deferred-work.md additions preserved, run continues), and typed
escalations — `CRITICAL` pauses the run and notifies you (desktop + `ATTENTION`
file), `PREFERENCE` is journaled and the run continues.

## Policy (`.automator/policy.toml`)

```toml
[gates]
mode = "per-epic"          # none | per-epic | per-story-spec-approval
retrospective = "notify"

[limits]
max_review_cycles = 3
max_dev_attempts = 2
session_timeout_min = 45
max_tokens_per_story = 2000000

[verify]
commands = ["pytest -q", "ruff check ."]

[adapter]
name = "claude"            # CLI profile: claude | codex | gemini | custom
model_dev = ""             # empty = CLI default
model_review = ""
# extra_args replaces the profile's default bypass flags when set:
# extra_args = ["--permission-mode", "bypassPermissions"]
```

Gate modes: `none` runs everything unattended; `per-epic` (default) pauses at
epic boundaries; `per-story-spec-approval` pauses after each spec is written so
you approve it before implementation is reviewed.

## Run state

Everything about a run lives in `.automator/runs/<run-id>/` (gitignored):
`state.json` (resumable engine state), `journal.jsonl` (every decision),
`events/` (hook signals), `tasks/<id>/` (per-session prompt + result +
escalations), `logs/` (raw pane output, debugging only), `deferred/`
(stashed specs from deferred stories), `ATTENTION` (human-readable alerts).

Token usage is read from each CLI's local session transcript (selected by the
profile's `usage_parser`) and aggregated per story (`bmad-auto status`).

## Other coding CLIs

One generic driver (`adapters/generic_tmux.py`) runs any coding CLI that fits
the tmux-injection + hook-signal transport; everything CLI-specific lives in a
declarative **profile** (`adapters/profile.py`). Built-in profiles ship as
TOML in `automator/data/profiles/`:

| Profile | Status | Notes |
|---|---|---|
| `claude` | supported | reference implementation |
| `codex` | supported, E2E-verified | Codex ≥ 0.139. No slash expansion in the initial prompt — the profile renders `$skill-name` mentions (plus a "use subagents as needed" nudge) instead. No SessionEnd hook; window-death fallback covers crashes. |
| `gemini` | supported, E2E-verified | Gemini CLI ≥ 0.46 (hooks on by default since then). Launches with `-i` to stay interactive; `AfterAgent` maps to canonical Stop. Usage parser validated against real chat logs. |

Heads-up on budgets: both Codex and Gemini report large cache-read counts
(~0.9–1.8M on a trivial story), and `limits.max_tokens_per_story` counts
them — size the budget accordingly.

Shared prerequisites: the BMAD skills must be present in `.agents/skills/`
(all three CLIs read it), and each CLI must have been run once interactively
in the project for auth/trust — `bmad-auto init --cli codex --cli gemini`
registers the hook relay and prints the per-CLI first-run steps.

**Adding a CLI without touching Python:** drop a TOML file in
`<project>/.automator/profiles/<name>.toml` (same fields as the built-ins:
binary, `prompt_template`, bypass flags, a `[hooks]` block picking one of the
config dialects `claude-settings-json` / `codex-hooks-json` /
`gemini-settings-json`, and a native→canonical event map). The hook relay
script and orchestrator are CLI-agnostic — each registration passes the
canonical event name as the script argument. A CLI whose hook config clones
one of the existing dialects (the ecosystem trend) needs nothing else; a
genuinely different transport gets its own adapter class instead (see the
opencode HTTP+SSE design stub in `adapters/opencode_http.py`).

Cursor CLI is currently blocked on two gaps, for whoever picks it up: token
usage is not exposed anywhere (hooks, JSON output, or on-disk chats), and
slash-command expansion of the initial prompt argument is unverified — its
`sessionStart`/`stop` hooks do fire in the CLI, so a profile using the
window-death fallback plus `usage_parser = "none"` is feasible.

## Development

```bash
pip install -e ".[dev]"
pytest -q            # unit + engine scenarios (mock adapter) + tmux integration
ruff check src tests
```
