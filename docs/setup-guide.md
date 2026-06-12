# Setup guide

This module is two things: the four `bmad-auto-*` skills and the `bmad-automator`
orchestrator tool (the Python program that actually drives the loop). The skills do
nothing on their own — the orchestrator is what spawns the fresh coding-CLI sessions
that invoke `bmad-auto-dev`, `bmad-auto-review`, and `bmad-auto-sweep`, watches their
hook signals, and verifies their artifacts. Installing the tool is part of setup, not
an optional extra.

There are two ways the skills land in a project: the **BMAD-method installer**
(recommended), or a **manual copy** from a clone of this repo. Both end at the same
place — the `/bmad-auto-setup` skill, which installs the tool, picks which coding CLIs
to drive, and bootstraps the project. For the one-page summary, see the
[Installing the skill module](../README.md#installing-the-skill-module) section of the
README.

## Installed via the BMAD-method installer? (recommended)

The BMAD-method installer copies the four `bmad-auto-*` skill directories into your
project. It does **not** carry the orchestrator tool — the installer copies only skill
directories, not their sibling files, so the tool can't ride along in the skill folder.
It is installed separately from Git by the setup skill. The canonical source is
<https://github.com/pbean/bmad-automator>.

After the installer runs, complete setup with one command:

```bash
claude "/bmad-auto-setup accept all defaults"
```

`/bmad-auto-setup` is one-shot. It:

1. Merges the module's config into `_bmad/config.yaml` (+ personal settings into the
   gitignored `_bmad/config.user.yaml`) and registers its help entries in
   `_bmad/module-help.csv`.
2. Installs the `bmad-automator` tool from Git (see
   [Installing the tool and TUI](#installing-the-tool-and-tui)).
3. Asks **which coding CLI(s)** the orchestrator should drive, then runs `bmad-auto init`
   to register hooks + write the `.automator/policy.toml` template + add gitignore entries
   (see [Choosing which CLIs to drive](#choosing-which-clis-to-drive) and
   [Initializing CLIs other than claude](#initializing-clis-other-than-claude)).
4. Runs `bmad-auto validate` as a preflight (see [Verify](#verify)).
5. Cleans up the legacy installer package directories under `_bmad/`, leaving only config.

Run `/bmad-auto-setup` with plain prompts if you want to choose interactively — e.g.
`claude "/bmad-auto-setup cli: claude, codex"` to preselect the CLIs.

## Manual install (repo clone / dev setup)

If you are working from a clone of this repo instead of the installer, copy the skill
folders into the trees the CLIs read, then install the tool in editable mode:

```bash
cp -r skills/bmad-auto-* /path/to/project/.claude/skills/
cp -r skills/bmad-auto-* /path/to/project/.agents/skills/   # codex/gemini only
pip install -e ".[tui]"                                      # the orchestrator tool + TUI
claude "/bmad-auto-setup accept all defaults"               # register config + bootstrap
```

The skills must be installed together: `bmad-auto-review` writes deferred-work entries per
`bmad-auto-dev/deferred-work-format.md` (a sibling skill directory).

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
python3 -m pip install --upgrade "bmad-automator[tui] @ git+https://github.com/pbean/bmad-automator.git"
```

**Tool first, TUI later (separately):** install the core without the extra, then add the
dashboard whenever you want it by re-running the same command **with** `[tui]`:

```bash
# core tool only
python3 -m pip install --upgrade "bmad-automator @ git+https://github.com/pbean/bmad-automator.git"

# add the TUI later — pip installs the extra's deps in place, no reinstall of the core
python3 -m pip install --upgrade "bmad-automator[tui] @ git+https://github.com/pbean/bmad-automator.git"
```

Until the extra is present, `bmad-auto tui` prints a clear error
(`the TUI requires optional dependencies — pip install 'bmad-automator[tui]'`) rather than
failing obscurely.

If pip reports an **externally-managed environment** (PEP 668) or a permission error, do
**not** force it with `--break-system-packages`. Instead install into an activated virtualenv,
or:

```bash
python3 -m pip install --user "bmad-automator[tui] @ git+https://github.com/pbean/bmad-automator.git"
# or, isolated (the [tui] extra is included):
pipx install "bmad-automator[tui] @ git+https://github.com/pbean/bmad-automator.git"
```

Confirm with `bmad-auto --version`.

## Initializing CLIs other than claude

`bmad-auto init` registers hooks per CLI. The `--cli` flag is repeatable — pass it once per
CLI you want to drive:

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

### Skill-location gotcha

`claude` reads skills from `.claude/skills/`; `codex` and `gemini` read from `.agents/skills/`.
If you selected `codex` or `gemini`, make sure the `bmad-auto-*` skills are installed in
`.agents/skills/` too, not only `.claude/skills/`.

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
