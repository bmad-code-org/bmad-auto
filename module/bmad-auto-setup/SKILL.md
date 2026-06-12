---
name: "bmad-auto-setup"
description: Sets up BMAD Automator Skills module in a project. Use when the user requests to 'install bmad-auto module', 'configure BMAD Automator Skills', or 'setup BMAD Automator Skills'.
---

# Module Setup

## Overview

Installs and configures a BMad module into a project. This module is special: alongside the four automation skills it ships the **bmad-auto orchestrator tool** (the Python program that drives the loop) bundled in the sibling `tool/` directory. So setup does two jobs — (1) register module config + help entries, and (2) install the orchestrator tool and bootstrap the project so it is ready to run.

Module identity (name, code, version) comes from `./assets/module.yaml`. Collects user preferences and writes them to three files:

- **`{project-root}/_bmad/config.yaml`** — shared project config: core settings at root (e.g. `output_folder`, `document_output_language`) plus a section per module with metadata and module-specific values. User-only keys (`user_name`, `communication_language`) are **never** written here.
- **`{project-root}/_bmad/config.user.yaml`** — personal settings intended to be gitignored: `user_name`, `communication_language`, and any module variable marked `user_setting: true` in `./assets/module.yaml`. These values live exclusively here.
- **`{project-root}/_bmad/module-help.csv`** — registers module capabilities for the help system.

Both config scripts use an anti-zombie pattern — existing entries for this module are removed before writing fresh ones, so stale values never persist.

`{project-root}` is a **literal token** in config _values_ (the data written into the files above) — never substitute it there. It signals to the consuming LLM that the value is relative to the project root, not the skill root. **This does not apply to the filesystem path _arguments_ passed to the scripts below** (the `--*-path`, `--*-dir`, and `--target` arguments): those are real paths, so you **must** resolve `{project-root}` to the actual project root before running, or the scripts will write to a literal `{project-root}/` directory under the skill folder. The scripts reject an unresolved token with an error.

## On Activation

1. Read `./assets/module.yaml` for module metadata and variable definitions (the `code` field is the module identifier)
2. Check if `{project-root}/_bmad/config.yaml` exists — if a section matching the module's code is already present, inform the user this is an update
3. Check for per-module configuration at `{project-root}/_bmad/bauto/config.yaml` and `{project-root}/_bmad/core/config.yaml`. If either file exists:
   - If `{project-root}/_bmad/config.yaml` does **not** yet have a section for this module: this is a **fresh install**. Inform the user that installer config was detected and values will be consolidated into the new format.
   - If `{project-root}/_bmad/config.yaml` **already** has a section for this module: this is a **legacy migration**. Inform the user that legacy per-module config was found alongside existing config, and legacy values will be used as fallback defaults.
   - In both cases, per-module config files and directories will be cleaned up after setup.

If the user provides arguments (e.g. `accept all defaults`, `--headless`, or inline values like `user name is BMad, I speak Swahili`), map any provided values to config keys, use defaults for the rest, and skip interactive prompting. Still display the full confirmation summary at the end.

## Collect Configuration

Ask the user for values. Show defaults in brackets. Present all values together so the user can respond once with only the values they want to change (e.g. "change language to Swahili, rest are fine"). Never tell the user to "press enter" or "leave blank" — in a chat interface they must type something to respond.

**Default priority** (highest wins): existing new config values > legacy config values > `./assets/module.yaml` defaults. When legacy configs exist, read them and use matching values as defaults instead of `module.yaml` defaults. Only keys that match the current schema are carried forward — changed or removed keys are ignored.

**Core config** (only if no core keys exist yet): `user_name` (default: BMad), `communication_language` and `document_output_language` (default: English — ask as a single language question, both keys get the same answer), `output_folder` (default: `{project-root}/_bmad-output`). Of these, `user_name` and `communication_language` are written exclusively to `config.user.yaml`. The rest go to `config.yaml` at root and are shared across all modules.

**Module config**: Read each variable in `./assets/module.yaml` that has a `prompt` field. Ask using that prompt with its default value (or legacy value if available).

## Write Files

Write a temp JSON file with the collected answers structured as `{"core": {...}, "module": {...}}` (omit `core` if it already exists). Values inside this JSON keep the literal `{project-root}` token. Then run both scripts — they can run in parallel since they write to different files.

In the commands below, replace `{project-root}` in every path argument with the actual project root (e.g. `/home/me/myapp`) before running — these are filesystem paths, not config values.

```bash
python3 ./scripts/merge-config.py --config-path "{project-root}/_bmad/config.yaml" --user-config-path "{project-root}/_bmad/config.user.yaml" --module-yaml ./assets/module.yaml --answers {temp-file} --legacy-dir "{project-root}/_bmad"
python3 ./scripts/merge-help-csv.py --target "{project-root}/_bmad/module-help.csv" --source ./assets/module-help.csv --legacy-dir "{project-root}/_bmad" --module-code bauto
```

Both scripts output JSON to stdout with results. If either exits non-zero, surface the error and stop. The scripts automatically read legacy config values as fallback defaults, then delete the legacy files after a successful merge. Check `legacy_configs_deleted` and `legacy_csvs_deleted` in the output to confirm cleanup.

Run `./scripts/merge-config.py --help` or `./scripts/merge-help-csv.py --help` for full usage.

## Create Output Directories

After writing config, create any output directories that were configured. For filesystem operations only (such as creating directories), resolve the `{project-root}` token to the actual project root and create each path-type value from `config.yaml` that does not yet exist — this includes `output_folder` and any module variable whose value starts with `{project-root}/`. The paths stored in the config files must continue to use the literal `{project-root}` token; only the directories on disk should use the resolved paths. Use `mkdir -p` or equivalent to create the full path.

## Install the Orchestrator Tool

This module ships the **bmad-auto orchestrator** — the Python program that actually drives the loop — bundled in the sibling `tool/` directory (`../tool` relative to this skill). The four skills do nothing on their own: the orchestrator is what spawns the fresh Claude Code sessions that invoke `bmad-auto-dev`, `bmad-auto-review`, and `bmad-auto-sweep`, watches their hook signals, and verifies their artifacts. Installing the tool is therefore part of setup, not an optional extra.

Unless the user explicitly asked to skip it (e.g. `skills only` / `--no-tool`), install and bootstrap now. In the commands below, resolve `<skill-dir>` to this skill's actual directory and `{project-root}` to the real project path before running.

1. **Check whether it's already installed:** run `bmad-auto --version`. If it succeeds, the tool is already on PATH — tell the user the detected version and ask whether to reinstall/upgrade from the bundled copy. If they decline, skip to step 3.

2. **Install from the bundled source** (the `[tui]` extra pulls in the Textual dashboard so `bmad-auto tui` works):

   ```bash
   python3 -m pip install "<skill-dir>/../tool[tui]"
   ```

   If pip reports an **externally-managed environment** (PEP 668) or a permission error, do **not** force or `--break-system-packages` it. Surface the message and let the user pick one of:
   - install into their active virtualenv (recommended if they have one activated), or
   - `python3 -m pip install --user "<skill-dir>/../tool[tui]"`, or
   - `pipx install "<skill-dir>/../tool[tui]"` (isolated; includes the TUI extra).

   Re-run with their choice, then confirm with `bmad-auto --version`.

3. **Bootstrap the project** — install the coding-CLI hooks, the `.automator/policy.toml` template, and the gitignore entry (idempotent).

   First decide **which coding CLI(s)** the orchestrator should drive. The three supported adapters are `claude` (default), `codex`, and `gemini`. Hooks are registered per CLI, so the choice matters — register every CLI you intend to use for dev/review/triage. Ask the user (unless they already specified it in their setup args, e.g. `cli: claude, codex`, or accepted defaults — then default to `claude` only):

   > "Which coding CLI(s) should the orchestrator drive — `claude`, `codex`, and/or `gemini`? You can pick more than one. [claude]"

   Build the command with one `--cli <name>` per selected CLI (the flag is repeatable):

   ```bash
   # claude only (default)
   bmad-auto init --project "{project-root}" --cli claude

   # multiple, e.g. claude + codex + gemini
   bmad-auto init --project "{project-root}" --cli claude --cli codex --cli gemini
   ```

   Names must be exactly `claude`, `codex`, or `gemini` — `init` errors on an unknown profile and lists the valid ones. `init` prints any one-time first-run notes per CLI (e.g. start `claude` once in the project and accept the workspace-trust + hooks-approval dialogs before `bmad-auto run` — spawned sessions can't answer first-run dialogs). Relay those notes to the user.

   **Note on skill location:** `claude` reads skills from `.claude/skills/`; `codex` and `gemini` read from `.agents/skills/`. If the user selected `codex` or `gemini`, make sure the `bmad-auto-*` skills are installed in `.agents/skills/` as well, not only `.claude/skills/`.

4. **Preflight** — verify config, sprint-status, git, tmux, and the coding CLI:

   ```bash
   bmad-auto validate --project "{project-root}"
   ```

   `validate` exits non-zero when the project isn't fully ready (e.g. no `sprint-status.yaml` yet, or `bmad-sprint-planning` hasn't run). On a fresh project that is **expected** — report its findings to the user as a readiness checklist, not as an install failure.

5. **Point the user at per-role adapter config.** `--cli` in step 3 only registers _hooks_ for each CLI. Which CLI actually **runs** each stage is governed by `{project-root}/.automator/policy.toml`, written from a template by `init`. The `[adapter] name` (default `claude`) applies to every stage; optional `[adapter.dev]`, `[adapter.review]`, and `[adapter.triage]` tables override individual stages (each takes its own `name` and `extra_args`). So a mixed setup — e.g. `claude` for dev, `codex` for review — needs both the hooks registered (step 3) **and** the role pointed at that CLI in `policy.toml`:

   ```toml
   [adapter]
   name = "claude"        # default for all stages

   [adapter.review]
   name = "codex"         # review runs on codex instead
   ```

   Tell the user where the file is and that any CLI named in `policy.toml` must also have been registered with `--cli` in step 3 (re-run `bmad-auto init --cli <name>` to add one later). Leave `policy.toml` untouched if they only use a single CLI — the default is correct.

## Cleanup Legacy Directories

After both merge scripts complete successfully, remove the installer's package directories. Skills and agents in these directories are already installed at `.claude/skills/` — the `_bmad/` directory should only contain config files.

As with the merge scripts, replace `{project-root}` in the `--bmad-dir` and `--skills-dir` path arguments with the actual project root before running.

```bash
python3 ./scripts/cleanup-legacy.py --bmad-dir "{project-root}/_bmad" --module-code bauto --also-remove _config --skills-dir "{project-root}/.claude/skills"
```

The script verifies that every skill in the legacy directories exists at `.claude/skills/` before removing anything. Directories without skills (like `_config/`) are removed directly. If the script exits non-zero, surface the error and stop. Missing directories (already cleaned by a prior run) are not errors — the script is idempotent.

Check `directories_removed` and `files_removed_count` in the JSON output for the confirmation step. Run `./scripts/cleanup-legacy.py --help` for full usage.

## Confirm

Use the script JSON output to display what was written — config values set (written to `config.yaml` at root for core, module section for module values), user settings written to `config.user.yaml` (`user_keys` in result), help entries added, fresh install vs update. Also report the **tool install**: the installed `bmad-auto --version`, that `bmad-auto init` registered hooks/policy/gitignore for the selected coding CLI(s) (name each one — e.g. "hooks registered for claude, codex"), and the `bmad-auto validate` preflight result (pass, or the readiness checklist of what's still missing). If legacy files were deleted, mention the migration. If legacy directories were removed, report the count and list (e.g. "Cleaned up 106 installer package files from bmb/, core/, \_config/ — skills are installed at .claude/skills/"). Then display the `module_greeting` from `./assets/module.yaml` to the user.

## Outcome

Once the user's `user_name` and `communication_language` are known (from collected input, arguments, or existing config), use them consistently for the remainder of the session: address the user by their configured name and communicate in their configured `communication_language`.
