# BMAD Automator module (`bauto`)

A BMAD module pairing the automation skills with the
[bmad-auto orchestrator tool](https://github.com/pbean/bmad-automator) (the
Python program that drives the loop). The skills can be installed by the BMAD
installer, or laid down by `bmad-auto init` (the orchestrator's wheel **bundles**
them); either way `bmad-auto-setup` installs the `bmad-automator` package from its
Git repository, so installing this module gives you a working system — skills
plus the orchestrator that invokes them. Standard BMAD installs are never
modified; the skills are automator-owned forks maintained against their upstream
counterparts.

| Component           | Forked from          | Role                                                                                                                                              |
| ------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bmad-automator`    | — (this repo, Git)   | the orchestrator: ralph-loop, hooks, tmux adapters, TUI. CLI `bmad-auto`. Installed by `bmad-auto-setup` from Git.                                |
| `bmad-auto-dev`     | `bmad-quick-dev`     | unattended implementation: story key / feedback file / dw-bundle → spec + code + result.json                                                      |
| `bmad-auto-review`  | `bmad-code-review`   | unattended adversarial review of a dev spec in a fresh context                                                                                    |
| `bmad-auto-resolve` | — (automator-native) | interactive CRITICAL-escalation resolution: a human disambiguates a frozen spec so a paused story can be re-driven (`/bmad-auto-resolve <story>`) |
| `bmad-auto-sweep`   | — (automator-native) | read-only deferred-work ledger triage                                                                                                             |
| `bmad-auto-setup`   | — (scaffolded)       | registers the module in `_bmad/config.yaml` + `module-help.csv`, **installs the orchestrator tool from Git**, runs `bmad-auto init` + `validate`  |

## Install into a project

The orchestrator tool now bundles these skills, so `bmad-auto init` lays them
down for you:

```bash
uv tool install "bmad-automator[tui] @ git+https://github.com/pbean/bmad-automator.git"
bmad-auto init --project /path/to/project --cli claude   # add --cli codex/gemini as needed
claude "/bmad-auto-setup accept all defaults"            # registers _bmad/ config + help
```

`bmad-auto init` installs the `bmad-auto-*` skills into `.claude/skills/`
(claude) and/or `.agents/skills/` (codex/gemini), registers hooks, writes
`.automator/policy.toml`, and gitignores the runs dir. Existing skill dirs are
left untouched (`--force-skills` to overwrite, `--no-skills` to skip).
`bmad-auto-setup` is one-shot for the BMAD-side wiring: it merges config + help
entries, ensures the tool is installed, then runs `bmad-auto init` and
`bmad-auto validate` (preflight).

The skills must be installed **together**: `bmad-auto-review` appends
deferred-work entries per `bmad-auto-dev/deferred-work-format.md` (sibling
skill directory). Requires the BMad Method (bmm) module
(`_bmad/bmm/config.yaml`) and a `sprint-status.yaml` from
`bmad-sprint-planning`.

`_bmad/custom/<skill-name>.toml` customization overrides are keyed by skill
directory name — duplicate any `bmad-quick-dev.toml` / `bmad-code-review.toml`
overrides as `bmad-auto-dev.toml` / `bmad-auto-review.toml`.

## Maintaining the forks

- This directory (`src/automator/data/skills/`) is **canonical** for the skills
  and is bundled into the wheel as package data, so `bmad-auto init` can install
  them. The repo's `.claude/skills/` and `.agents/skills/` hold dev-workspace
  copies; `tests/test_module_skills_sync.py` fails if they drift. After editing
  here, re-copy the skill dirs into both trees.
- The orchestrator tool is **not** bundled in the skill dirs — the BMAD installer
  copies only the skill directories, so a sibling `tool/` would never reach an
  installed project. `bmad-auto-setup` installs the `bmad-automator` package from
  <https://github.com/pbean/bmad-automator> (`src/automator`, `pyproject.toml`
  are canonical at the repo root). (The skills, by contrast, ride along inside
  the package wheel.)
- The forks keep the upstream file structure. To pull upstream improvements:
  `diff -r <bmad-install>/bmad-quick-dev bmad-auto-dev`, merge manually.
- Do **not** rename the result.json `workflow` values (`"quick-dev"`,
  `"code-review"`, `"deferred-sweep-triage"`) or the `plan-code-review` route —
  they are machine contracts validated by the orchestrator, not skill names.

Validate after changes (from the repo root):

```bash
python3 .claude/skills/bmad-module-builder/scripts/validate-module.py src/automator/data/skills
```
