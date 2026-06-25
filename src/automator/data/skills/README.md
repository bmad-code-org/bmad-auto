# BMAD Auto module (`bauto`)

A BMAD module pairing the automation skills with the
[bmad-auto orchestrator tool](https://github.com/bmad-code-org/bmad-auto) (the
Python program that drives the loop). The skills can be installed by the BMAD
installer, or laid down by `bmad-auto init` (the orchestrator's wheel **bundles**
them); either way `bmad-auto-setup` installs the `bmad-auto` package from its
Git repository, so installing this module gives you a working system — skills
plus the orchestrator that invokes them. Standard BMAD installs are never
modified; the skills are automator-owned, standalone or automator-native (see
the table below).

| Component           | Forked from          | Role                                                                                                                                              |
| ------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bmad-auto`         | — (this repo, Git)   | the orchestrator: ralph-loop, hooks, tmux adapters, TUI. CLI `bmad-auto`. Installed by `bmad-auto-setup` from Git.                                |
| `bmad-auto-resolve` | — (automator-native) | interactive CRITICAL-escalation resolution: a human disambiguates a frozen spec so a paused story can be re-driven (`/bmad-auto-resolve <story>`) |
| `bmad-auto-sweep`   | — (automator-native) | read-only deferred-work ledger triage; owns the canonical `deferred-work-format.md`                                                               |
| `bmad-auto-setup`   | — (scaffolded)       | registers the module in `_bmad/config.yaml` + `module-help.csv`, **installs the orchestrator tool from Git**, runs `bmad-auto init` + `validate`  |

The **inner dev primitive is the upstream `bmad-dev-auto` skill** (BMAD-METHOD's
generic unattended dev session). It is **not** owned or bundled here — the
orchestrator drives it as an external skill that must already be installed
(by the BMad installer / bmm-core). The automator synthesizes its `result.json`
from the spec the session leaves on disk (see `automator.devcontract`). The skill
self-reviews inline (Blind + Edge-Case hunters in its step-04) and commits its own
work each iteration; the orchestrator's **follow-up review is just a re-invocation
of `bmad-dev-auto` on the done spec** (BMAD-METHOD #2508 routes a `done` spec to a
fresh review pass), so there is no separate review skill.

## Install into a project

The orchestrator tool now bundles these skills, so `bmad-auto init` lays them
down for you:

```bash
uv tool install "bmad-auto[tui] @ git+https://github.com/bmad-code-org/bmad-auto.git"
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

The skills must be installed **together**: `bmad-auto-sweep` owns the canonical
`deferred-work-format.md` that the ledger normalizes to, and the upstream
`bmad-dev-auto` dev session must also be present (it appends flat deferred-work
entries the orchestrator normalizes on sweep). Requires the BMad Method (bmm)
module (`_bmad/bmm/config.yaml`) and a `sprint-status.yaml` from
`bmad-sprint-planning`.

`_bmad/custom/<skill-name>.toml` customization overrides are keyed by skill
directory name.

## Maintaining the skills

- This directory (`src/automator/data/skills/`) is **canonical** for the skills
  and is bundled into the wheel as package data, so `bmad-auto init` can install
  them. The repo's `.claude/skills/` and `.agents/skills/` hold dev-workspace
  copies; `tests/test_module_skills_sync.py` fails if they drift. After editing
  here, re-copy the skill dirs into both trees.
- The orchestrator tool is **not** bundled in the skill dirs — the BMAD installer
  copies only the skill directories, so a sibling `tool/` would never reach an
  installed project. `bmad-auto-setup` installs the `bmad-auto` package from
  <https://github.com/bmad-code-org/bmad-auto> (`src/automator`, `pyproject.toml`
  are canonical at the repo root). (The skills, by contrast, ride along inside
  the package wheel.)
- The inner dev primitive `bmad-dev-auto` is **not** maintained here — it is the
  upstream bmm-core skill, driven unmodified. Nothing in this directory mirrors
  it; the orchestrator adapts to it via `automator.devcontract`.
- Do **not** rename the result.json `workflow` values — they are machine
  contracts the orchestrator validates, not skill names:
  - dev → `"auto-dev"` (checked by `verify.DEV_WORKFLOW` in
    `verify_dev` / `verify_dev_bundle`; the orchestrator forges this value in
    `devcontract` when synthesizing the dev result from the spec).
  - sweep triage / migrate → `"deferred-sweep-triage"` / `"deferred-sweep-migrate"`
    (checked in `sweep.py`).

Validate after changes (from the repo root):

```bash
python3 .claude/skills/bmad-module-builder/scripts/validate-module.py src/automator/data/skills
```
