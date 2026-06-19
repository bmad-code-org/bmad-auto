# Setup guide

This module is two things: the five `bmad-auto-*` skills and the `bmad-automator`
orchestrator tool (the Python program that actually drives the loop). The skills do
nothing on their own — the orchestrator is what spawns the fresh coding-CLI sessions
that invoke `bmad-auto-dev`, `bmad-auto-review`, `bmad-auto-sweep`, and
`bmad-auto-resolve`, watches their hook signals, and verifies their artifacts. Installing the tool is part of setup, not
an optional extra.

There are two ways the skills land in a project. The orchestrator's wheel **bundles**
the five skills, so the simplest path is **pip + `bmad-auto init`**, which installs them
itself. Alternatively the **BMAD-method installer** copies them. Either way the
`/bmad-auto-setup` skill registers the `_bmad/` config, ensures the tool is installed,
picks which coding CLIs to drive, and bootstraps the project. For the one-page summary,
see the [Installing the skill module](../README.md#installing-the-skill-module) section
of the README.

## Installed via the BMAD-method installer? (recommended)

The BMAD-method installer copies the five `bmad-auto-*` skill directories into your
project. It does **not** carry the orchestrator tool — the installer copies only skill
directories, not their sibling files, so the tool can't ride along in the skill folder.
It is installed separately from Git by the setup skill. The canonical source is
<https://github.com/pbean/bmad-automator>. (Going the other way, the tool's wheel
bundles the skills, so `bmad-auto init` can install them without the BMAD installer —
when the installer already placed them, `init` simply skips the existing copies.)

After the installer runs, complete setup with one command:

```bash
claude "/bmad-auto-setup accept all defaults"
```

`/bmad-auto-setup` handles both first-time setup and later upgrades — re-run it any time. It:

1. Merges the module's config into `_bmad/config.yaml` (+ personal settings into the
   gitignored `_bmad/config.user.yaml`) and registers its help entries in
   `_bmad/module-help.csv`.
2. Installs **or upgrades** the `bmad-automator` tool from Git (see
   [Installing the tool and TUI](#installing-the-tool-and-tui)). On an upgrade it runs
   `uv tool upgrade bmad-automator --reinstall`.
3. Asks **which coding CLI(s)** the orchestrator should drive, then runs `bmad-auto init`
   to install the `bmad-auto-*` skills + register hooks + write the `.automator/policy.toml`
   template + add gitignore entries (see [Choosing which CLIs to drive](#choosing-which-clis-to-drive)
   and [Initializing CLIs other than claude](#initializing-clis-other-than-claude)). On an
   upgrade it passes `--force-skills` so the per-project skill copies are refreshed.
4. Runs `bmad-auto validate` as a preflight (see [Verify](#verify)).
5. Cleans up the legacy installer package directories under `_bmad/`, leaving only config.

Run `/bmad-auto-setup` with plain prompts if you want to choose interactively — e.g.
`claude "/bmad-auto-setup cli: claude, codex"` to preselect the CLIs.

## Manual install (repo clone / dev setup)

If you are working from a clone of this repo, sync the project env and let
`bmad-auto init` lay down the skills (the canonical skills live at
`src/automator/data/skills/` and are bundled into the package):

```bash
uv sync --extra tui                                             # the orchestrator tool + TUI
uv run bmad-auto init --project /path/to/project --cli claude   # installs skills + hooks + policy
claude "/bmad-auto-setup accept all defaults"                   # register _bmad/ config + help
```

Add `--cli codex --cli gemini` to also populate `.agents/skills/`. The skills must be
installed together: `bmad-auto-review` writes deferred-work entries per
`bmad-auto-dev/deferred-work-format.md` (a sibling skill directory) — `init` always
installs them all (`bmad-auto-dev`, `bmad-auto-review`, `bmad-auto-resolve`,
`bmad-auto-sweep`, `bmad-auto-setup`).

## Choosing which CLIs to drive

The three supported adapters are `claude` (the default), `codex`, and `gemini`. You can
pick more than one — register every CLI you intend to use for dev, review, or sweep triage.

There are **two layers** here, and confusing them is the usual stumbling block:

- `bmad-auto init --cli <name>` registers the orchestrator's **hooks** for that CLI. Without
  registered hooks, a CLI can't signal the engine.
- `.automator/policy.toml` `[adapter]` selects which CLI actually **runs** each stage.

So a mixed setup — say `claude` for dev and `codex` for review — needs _both_: the hooks
registered for each CLI (`--cli claude --cli codex`) **and** the role pointed at that CLI in
`policy.toml`:

```toml
[adapter]
name = "claude"        # default for every stage

[adapter.review]
name = "codex"         # the review pass runs on codex instead
```

Any CLI named in `policy.toml` must also have been registered with `--cli`. To add one later,
re-run `bmad-auto init --cli <name>`. If you only use a single CLI, leave `policy.toml`
untouched — the default is correct.

## Installing the tool and TUI

The `[tui]` extra pulls in the Textual dashboard (`textual` + `tomlkit` + `pyte`) so
`bmad-auto tui` works. The core tool needs only `pyyaml`.

**Together (recommended):**

```bash
uv tool install "bmad-automator[tui] @ git+https://github.com/pbean/bmad-automator.git"
```

**Tool first, TUI later (separately):** install the core without the extra, then add the
dashboard whenever you want it by re-running the same command **with** `[tui]`:

```bash
# core tool only
uv tool install "bmad-automator @ git+https://github.com/pbean/bmad-automator.git"

# add the TUI later — re-run with the extra (uv upgrades the install in place)
uv tool install --upgrade "bmad-automator[tui] @ git+https://github.com/pbean/bmad-automator.git"
```

Until the extra is present, `bmad-auto tui` prints a clear error
(`the TUI requires optional dependencies — uv tool install 'bmad-automator[tui]'`) rather than
failing obscurely.

`uv tool install` drops `bmad-auto` into uv's own managed tool environment, so there's no
PEP 668 externally-managed conflict and no need for a virtualenv, `--user`, or `--break-system-packages`.

To upgrade later, the simplest path is to re-run `/bmad-auto-setup` (or `/bmad-auto-setup
upgrade`) — it detects the existing install, upgrades the tool with `uv tool upgrade
bmad-automator --reinstall` (the `--reinstall` is **required** for a git source — a plain
`uv tool upgrade` reuses the cached commit and won't pull new code), and re-lays the
per-project skills with `bmad-auto init --force-skills`. To do it by hand, run those two
commands yourself (see the [Upgrading](../README.md#upgrading) section of the README).

Confirm with `bmad-auto --version`.

## Initializing CLIs other than claude

`bmad-auto init` registers hooks and installs the bundled `bmad-auto-*` skills per CLI. The
`--cli` flag is repeatable — pass it once per CLI you want to drive:

```bash
# claude only (default)
bmad-auto init --project <project-root> --cli claude

# multiple, e.g. claude + codex + gemini
bmad-auto init --project <project-root> --cli claude --cli codex --cli gemini
```

Run with no `--cli` and `init` registers hooks for every CLI the `policy.toml` references,
so a dual-client setup that's already configured in policy needs no extra flags. Names must
be exactly `claude`, `codex`, or `gemini` — `init` errors on an unknown profile and lists the
valid ones.

### First-run notes

Each CLI needs a one-time interactive setup before the first `bmad-auto run`, because
spawned sessions can't answer first-run dialogs. `init` prints the relevant notes; relay
them to whoever owns the machine:

- **claude** — run `claude` once in the project and accept the workspace-trust + hooks-approval
  dialogs.
- **codex** — run `codex` once in the project and accept **both** prompts: workspace trust,
  then "Hooks need review → Trust all and continue" (untrusted hooks silently never fire).
  Requires Codex ≥ 0.139.
- **gemini** — authenticate once (browser OAuth or `GEMINI_API_KEY`). Requires Gemini CLI
  ≥ 0.46.

### Skill location

`claude` reads skills from `.claude/skills/`; `codex` and `gemini` read from `.agents/skills/`.
`init` installs the bundled `bmad-auto-*` skills into the right tree for each CLI you pass via
`--cli`, so selecting `codex`/`gemini` populates `.agents/skills/` automatically. It skips skill
dirs that already exist — pass `--force-skills` to overwrite a stale copy, or `--no-skills` to
manage them yourself.

## Verify

Preflight the project — config, sprint-status, git, tmux, and the coding CLI:

```bash
bmad-auto validate --project <project-root>
```

`validate` exits non-zero when the project isn't fully ready (e.g. no `sprint-status.yaml`
yet, or `bmad-sprint-planning` hasn't run). On a fresh project that is **expected** — read its
output as a readiness checklist, not an install failure.

For the dashboard itself, see [docs/tui-guide.md](tui-guide.md). For the full policy
reference, see the [Policy section](../README.md#policy-automatorpolicytoml) of the README.
